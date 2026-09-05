"""A gateway for a book we keep ourselves.

WHY THIS EXISTS. Razorpay Subscriptions is not enabled on this account --
`/subscriptions` and `/plans` return 401 on a key that reads `/payments` and
`/customers` fine -- and it cannot be enabled from here. That is not a reason
to leave the most valuable recovery loop unrunnable, because nothing about the
loop depends on Razorpay: what a subscription needs is a standing mandate and a
schedule, and both can live in a book we hold.

So this adapter backs a LOCAL subscription book. It creates its own recovery
artefacts, which also sidesteps the test-mode ceiling of 30 payment links that
the live account has already spent, and it can re-present against a mandate,
which the live account cannot.

WHAT MAKES THIS HONEST RATHER THAN A FAKE

    mode is LOCAL, never REAL. Every artefact and every capture says so, and
    the ledger carries it, so no reader can mistake one of these for money that
    moved through Razorpay. The Rs 28,433 measured on the live account stays
    the only REAL recovery in this project.

    THE OUTCOME IS NOT A COIN FLIP. A re-present captures when the money is
    there, and the book says when the money arrives: each subscription carries
    `funds_return_at`, written at seed time from its decline class. Insufficient
    funds resolves on payday; an expired card never resolves at all. So the
    result of any charge is a function of the book and the clock, both of which
    a reader can inspect -- run it twice and get the same answer, read the file
    and predict it.

That is also the real dynamic this loop exists to exploit. A silent retry does
not persuade anybody; it is simply there when the balance returns. Modelling it
with a hidden random number would have thrown away the one thing the mandate
ladder is actually about.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import Capabilities, RecoveryArtefact, WebhookEvent

IST = timezone(timedelta(hours=5, minutes=30))
DATA = Path("data/live")


@dataclass
class LocalStore:
    """The book, on disk, so it survives between runs like a real one."""
    path: Path
    rows: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "LocalStore":
        rows: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    rows[r["id"]] = r
        return cls(path=path, rows=rows)

    def get(self, ref: str) -> dict | None:
        return self.rows.get(ref)


class LocalGateway:
    """Serves a subscription book this project owns."""

    name = "local"

    def __init__(self, env: dict | None = None,
                 book: Path | None = None,
                 artefacts: Path | None = None,
                 base_url: str = "https://recover.example.com"):
        self.env = env or {}
        self.book = LocalStore.load(
            Path(book) if book else DATA / "acme_subs.subscriptions.jsonl")
        self.artefacts = (Path(artefacts) if artefacts
                          else DATA / "acme_subs.artefacts.jsonl")
        self.base_url = base_url
        self._links = LocalStore.load(self.artefacts)
        self.clock: datetime | None = None

    # ------------------------------------------------------------ protocol
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="local",
            # A link we mint ourselves has no method restriction either -- not
            # because of a provider limitation but because there is nothing
            # behind it enforcing one. Claiming otherwise would be the easy lie.
            can_restrict_methods=False,
            can_notify=False,          # no telephony or mail behind this book
            can_charge_saved_instrument=True,
            webhook_signature=None,
            max_recovery_artefacts=None,
            notes="local subscription book; mandates are ours, so silent "
                  "re-presents execute. Nothing here touches Razorpay and no "
                  "artefact is REAL money.",
        )

    def _now(self) -> datetime:
        return self.clock or datetime.now(IST)

    def _append(self, path: Path, row: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def create_recovery(self, amount_paise: int, description: str,
                        exclude_methods: list[str] | None = None,
                        notify: dict | None = None,
                        customer: dict | None = None,
                        notes: dict | None = None,
                        dry_run: bool = False) -> RecoveryArtefact:
        ref = f"plink_local_{len(self._links.rows) + 1:05d}"
        row = {"id": ref, "amount_paise": int(amount_paise),
               "description": description[:300],
               "reference": (notes or {}).get("reference"),
               "created_at": self._now().isoformat(), "status": "created",
               "amount_paid_paise": 0,
               "exclude_methods": list(exclude_methods or [])}
        if not dry_run:
            self._links.rows[ref] = row
            self._append(self.artefacts, row)
        return RecoveryArtefact(
            gateway=self.name, reference=ref,
            url=f"{self.base_url}/r/{ref}",
            amount_paise=amount_paise, mode="LOCAL", status="created",
            method_restriction_enforced=False,
            notified={},          # can_notify is False; nothing was sent
            detail=f"local recovery link, excluding {exclude_methods or 'nothing'}")

    def fetch_recovery(self, reference: str) -> RecoveryArtefact:
        r = self._links.get(reference)
        if r is None:
            raise KeyError(f"no local artefact {reference!r}")
        return RecoveryArtefact(
            gateway=self.name, reference=reference, url=None,
            amount_paise=int(r["amount_paise"]), mode="LOCAL",
            status=r.get("status", "created"),
            amount_paid_paise=int(r.get("amount_paid_paise") or 0),
            detail=r.get("description", ""))

    def verify_webhook(self, raw_body: bytes, headers: dict, secret: str,
                       now: datetime | None = None) -> WebhookEvent:
        raise NotImplementedError(
            "the local book receives no webhooks; outcomes are read back with "
            "fetch_recovery, the same way reconcile_links reads Razorpay")

    # ------------------------------------------------------------- mandate
    def charge_mandate(self, amount_paise: int, reference: str,
                       attempt_no: int = 0) -> RecoveryArtefact:
        """Re-present silently. Captures when the money has arrived.

        `funds_return_at` is in the book, written when it was seeded. A card
        recorded as dead has none and never captures however many times it is
        tried -- which is the ceiling the whole recovery-rate argument rests on,
        and it has to bind here or this loop would recover cases no ladder can.
        """
        sub = self.book.get(reference) or {}
        now = self._now()
        ref = f"pay_local_{reference[-6:]}_{attempt_no}"

        dead = not sub.get("mandate_live", True)
        when = sub.get("funds_return_at")
        arrived = False
        if when and not dead:
            arrived = now >= datetime.fromisoformat(when)

        if arrived:
            detail = "captured against the standing mandate"
        elif dead:
            detail = "the mandate is no longer chargeable (instrument dead)"
        else:
            detail = (f"declined: funds not yet available"
                      + (f" (expected {when[:10]})" if when else ""))

        return RecoveryArtefact(
            gateway=self.name, reference=ref, url=None,
            amount_paise=amount_paise, mode="LOCAL",
            status="paid" if arrived else "failed",
            amount_paid_paise=amount_paise if arrived else 0,
            detail=detail)

    # -------------------------------------------------------- book queries
    def mark_paid(self, artefact_ref: str, when: datetime) -> None:
        """A customer paid a link. Recorded so `fetch_recovery` sees it."""
        r = self._links.get(artefact_ref)
        if r is None or r.get("status") == "paid":
            return
        r["status"] = "paid"
        r["amount_paid_paise"] = r["amount_paise"]
        r["paid_at"] = when.isoformat()
        self._append(self.artefacts, dict(r, event="paid"))
