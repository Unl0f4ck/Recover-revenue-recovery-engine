"""Webhook ingestion. Event-driven instead of polling.

We poll: every pass fetches payments, orders and links and diffs them against
the ledger. That works, it is simple, and it is wrong in two ways that matter.
It is slow -- a customer who pays a recovery link at 09:00 is not recorded as
recovered until the next cron -- and it scales with the size of the book rather
than with the number of things that actually happened.

Etherlabs and ajithmanmu both ingest webhooks instead, and Etherlabs is
explicit about the three things that make verification correct. All three are
easy to get wrong and each has a comment below where it is handled:

    EXACT RAW BYTES         a re-serialised body never matches
    CONSTANT-TIME COMPARE   `==` leaks the signature prefix through timing
    SECRET ROTATION         retries arrive signed with the previous secret

THE DIFFERENCE FROM STRIPE. Razorpay signs only the body -- there is no
timestamp in the signature and therefore no tolerance window to enforce. Copying
Stripe's `t=...,v1=...` pattern would mean inventing a check with nothing to
check against. Replay protection here comes from deduplicating on
`x-razorpay-event-id`, which this module does, and that difference is recorded
rather than papered over.

WHAT AN EVENT IS ALLOWED TO DO. Deliberately narrow. A verified event can
record a recovery, close a sequence whose payment has resolved elsewhere, or
open nothing at all. It cannot CREATE a contact -- a webhook arriving at 03:00
must not cause an SMS at 03:00, so anything requiring an outbound step is left
for the next pass, where quiet hours and the ceilings apply.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..gateways import get_gateway
from ..gateways.base import WebhookEvent
from . import dunning as D
from . import ledger as L
from .campaign import rebuild
from .jsonl import read_jsonl
from .declines import load_declines

WEBHOOKS = Path("data/live/webhook_events.jsonl")


@dataclass
class Ingested:
    event_id: str
    kind: str
    reference: str | None
    applied: str                 # what we did
    duplicate: bool = False
    detail: str = ""
    extra: dict = field(default_factory=dict)


def _seen(event_id: str, path: Path | None = None) -> bool:
    if not event_id:
        return False
    return any(r.get("event_id") == event_id
               for r in read_jsonl(path or WEBHOOKS))


def _record(ev: WebhookEvent, applied: str, at: datetime, duplicate: bool,
            detail: str, path: Path | None = None) -> None:
    p = path or WEBHOOKS
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "at": at.isoformat(), "received_at": ev.at.isoformat(),
            "event_id": ev.event_id, "event": ev.event, "kind": ev.kind,
            "reference": ev.reference, "amount_paise": ev.amount_paise,
            "applied": applied, "duplicate": duplicate, "detail": detail,
        }) + "\n")


def verify(raw_body: bytes, headers: dict, secret: str,
           previous_secrets: list[str] | None = None,
           gateway=None) -> WebhookEvent:
    gw = gateway or get_gateway("razorpay")
    return gw.verify_webhook(raw_body, headers, secret,
                             previous_secrets=previous_secrets)


def _link_owner(artefact_id: str, path: Path | None = None) -> str | None:
    """Which sequence created this recovery link.

    A `payment_link.paid` event names the LINK, not the order the campaign is
    about. The ledger already records which sequence produced which artefact,
    so the mapping is a lookup rather than a guess.
    """
    for e in L.read(path):
        if e.get("execution_reference") == artefact_id:
            return e.get("reference")
    return None


def apply(ev: WebhookEvent, now: datetime, cfg: dict | None = None,
          path: Path | None = None, wh_path: Path | None = None) -> Ingested:
    """Act on a verified event. Idempotent on `event_id`.

    Providers retry, and a retry that is applied twice records a recovery
    twice. Deduplication is on the provider's own event id, which is what it is
    for.
    """
    cfg = cfg or load_declines()

    if _seen(ev.event_id, wh_path):
        return Ingested(ev.event_id, ev.kind, ev.reference, "ignored",
                        duplicate=True,
                        detail="already applied; provider retry")

    applied, detail = "ignored", ""

    if ev.kind == "recovery_paid" and ev.reference:
        owner = _link_owner(ev.reference, path)
        seq = rebuild(owner, cfg, path) if owner else None
        if seq is None:
            applied, detail = "ignored", "no sequence owns this link"
        elif L.has_succeeded(owner, path):
            applied, detail = "ignored", "already recorded as recovered"
        else:
            origin = next(e for e in L.events_for(owner, path)
                          if e.get("execution_reference") == ev.reference)
            n = min(int(origin["attempt_no"]), len(seq.steps) - 1)
            D.record_outcome(seq, seq.steps[n], D.AttemptOutcome(
                "SUCCEEDED", f"webhook {ev.event}: link {ev.reference} paid",
                execution_mode="REAL", execution_reference=ev.reference,
                amount_paise=ev.amount_paise), now, path)
            D.stop(seq, D.STOP_RECOVERED, now,
                   f"webhook {ev.event_id}", path)
            applied = "recovered"
            detail = f"Rs {ev.amount_paise/100:,.0f} on {owner}"

    elif ev.kind in ("recovery_expired", "recovery_cancelled") and ev.reference:
        owner = _link_owner(ev.reference, path)
        seq = rebuild(owner, cfg, path) if owner else None
        if seq is not None and not L.is_stopped(owner, path):
            # The artefact died. That does NOT end the campaign -- the next
            # rung can still issue a fresh link. Recorded so the timeline shows
            # why the customer's link stopped working.
            D.reconcile(seq, ev.event, now, path)
            applied, detail = "noted", f"{ev.event} on {owner}"

    elif ev.kind in ("payment_captured", "order_paid") and ev.reference:
        # The underlying payment resolved somewhere else entirely -- the
        # customer went back to checkout, or support took payment by hand.
        # medusa#16398 is exactly this hole: an unobserved terminal state means
        # the campaign keeps escalating at someone who has already paid.
        pay = ((ev.payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
        references = {ev.reference, pay.get("order_id"), pay.get("invoice_id")}
        for row in L.read(path):
            if row["event"] != L.SEQUENCE_OPENED:
                continue
            ref = row["reference"]
            if ref not in references and (row.get("extra") or {}).get("order_id") not in (references - {None}):
                continue
            seq = rebuild(ref, cfg, path)
            if seq is not None and not L.is_stopped(ref, path):
                D.stop(seq, D.STOP_TERMINAL_STATE, now,
                       f"webhook {ev.event}: paid outside the campaign", path)
                applied, detail = "closed", "underlying obligation paid outside campaign"

    elif ev.kind == "payment_failed":
        # A new failure. It is NOT opened here: opening a campaign means
        # deciding to contact someone, and that decision belongs to a pass
        # where the exposure floor, quiet hours and the caps all apply. The
        # event is recorded so the next pass finds it immediately.
        applied, detail = "queued", "new failure; the next pass will classify it"

    _record(ev, applied, now, False, detail, wh_path)
    return Ingested(ev.event_id, ev.kind, ev.reference, applied, False, detail)


def handle(raw_body: bytes, headers: dict, secret: str, now: datetime,
           previous_secrets: list[str] | None = None,
           cfg: dict | None = None, path: Path | None = None,
           wh_path: Path | None = None, gateway=None) -> Ingested:
    """Verify, then apply. The only entry point a web server should call."""
    ev = verify(raw_body, headers, secret, previous_secrets, gateway)
    return apply(ev, now, cfg, path, wh_path)


def read(path: Path | None = None) -> list[dict]:
    return read_jsonl(path or WEBHOOKS)


def summary(path: Path | None = None) -> dict:
    rows = read(path)
    by: dict[str, int] = {}
    for r in rows:
        by[r["applied"]] = by.get(r["applied"], 0) + 1
    return {"total": len(rows),
            "duplicates": sum(1 for r in rows if r.get("duplicate")),
            "by_action": sorted(by.items(), key=lambda kv: -kv[1])}
