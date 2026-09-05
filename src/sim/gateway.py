"""A payment gateway that mints links nobody has to pay for real.

Implements the same `Gateway` protocol as the Razorpay adapter, so the campaign
code cannot tell the difference -- `campaign.execute_step` already accepts a
`gateway=` argument and calls `create_recovery` on whatever it is handed.

THE CAPABILITIES ARE COPIED FROM RAZORPAY ON PURPOSE, including the two that
hurt. It is tempting to let a simulated gateway do the things the real account
cannot: restrict payment methods, charge a saved instrument. Both would make
the batch look better and both would be a lie, because the ladder's silent
rungs would start converting and the whole mandate argument in
docs/RECOVERY_RATE.md would quietly evaporate. A simulator that grants itself
capabilities the production account lacks is measuring a system nobody has.

The one honest divergence is `max_recovery_artefacts`: test mode caps links per
business at 30, which is a sandbox quota rather than a property of the payments
system being modelled. A simulated batch of two hundred cases is not
constrained by it, and pretending otherwise would model Razorpay's free tier
rather than the recovery problem.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.gateways.base import Capabilities, RecoveryArtefact, WebhookEvent


@dataclass
class SimLink:
    """One recovery artefact and its life. The gateway's whole state."""
    reference: str
    amount_paise: int
    created_at: datetime
    description: str = ""
    exclude_methods: list[str] = field(default_factory=list)
    customer: dict | None = None
    notes: dict = field(default_factory=dict)
    status: str = "created"          # created | paid | expired
    amount_paid_paise: int = 0
    paid_at: datetime | None = None


class SimGateway:
    """Stateful, in-memory, and deliberately dull.

    It creates links and reports their status. It never decides whether a link
    gets paid -- that is the customer model's job, and keeping the two apart is
    what stops the gateway quietly becoming the simulation.
    """

    name = "sim"

    def __init__(self, prefix: str = "plink_sim",
                 clock: datetime | None = None, mandate: bool = False,
                 mandate_outcome=None):
        self.links: dict[str, SimLink] = {}
        self.prefix = prefix
        self.clock = clock
        # A MERCHANT WITH SUBSCRIPTIONS ENABLED. Off by default, so the ordinary
        # batch keeps mirroring the live account exactly and cannot recover
        # money the real one could not have. Turned on only for the
        # subscription loop, where the entire question is what a mandate buys
        # -- docs/RECOVERY_RATE.md puts it at 19 points of recovery rate.
        self.mandate = mandate
        self.mandate_outcome = mandate_outcome
        self.charges: list[dict] = []
        self._n = 0

    # ------------------------------------------------------------ protocol
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="sim",
            can_restrict_methods=False,       # as Razorpay: the field is ignored
            can_notify=True,
            can_charge_saved_instrument=self.mandate,
            webhook_signature="hmac-sha256-rawbody",
            max_recovery_artefacts=None,
            notes=("simulated gateway; capabilities mirror the live Razorpay "
                   "account so the batch cannot recover money the real one "
                   "could not have"
                   + (" -- EXCEPT a stored mandate, enabled deliberately to "
                      "model a merchant who has Subscriptions turned on, "
                      "which this account does not" if self.mandate else "")),
        )

    def create_recovery(self, amount_paise: int, description: str,
                        exclude_methods: list[str] | None = None,
                        notify: dict | None = None,
                        customer: dict | None = None,
                        notes: dict | None = None,
                        dry_run: bool = False) -> RecoveryArtefact:
        self._n += 1
        ref = f"{self.prefix}_{self._n:05d}"
        at = self.clock or _now_from(notes)
        self.links[ref] = SimLink(
            reference=ref, amount_paise=amount_paise, created_at=at,
            description=description, exclude_methods=list(exclude_methods or []),
            customer=customer, notes=dict(notes or {}))
        return RecoveryArtefact(
            gateway=self.name, reference=ref,
            url=f"https://sim.invalid/l/{ref}",
            amount_paise=amount_paise, mode="SIMULATED", status="created",
            # False for the same reason Razorpay reports False: the exclusion
            # is recorded as intent and enforced by nobody.
            method_restriction_enforced=False,
            notified=dict(notify or {}),
            detail=f"simulated link excluding {exclude_methods or 'nothing'}")

    def fetch_recovery(self, reference: str) -> RecoveryArtefact:
        l = self.links.get(reference)
        if l is None:
            raise KeyError(f"no simulated link {reference!r}")
        return RecoveryArtefact(
            gateway=self.name, reference=l.reference, url=None,
            amount_paise=l.amount_paise, mode="SIMULATED", status=l.status,
            amount_paid_paise=l.amount_paid_paise,
            detail=l.description)

    def verify_webhook(self, raw_body: bytes, headers: dict, secret: str,
                       now: datetime | None = None) -> WebhookEvent:
        raise NotImplementedError(
            "the simulated gateway does not receive webhooks; outcomes are "
            "read back with fetch_recovery, the same way reconcile_links reads "
            "the live account")

    def charge_mandate(self, amount_paise: int, reference: str,
                       attempt_no: int = 0) -> RecoveryArtefact:
        """A silent re-present. Contacts nobody, spends no contact ceiling."""
        if not self.mandate:
            raise NotImplementedError("this simulated gateway holds no mandate")
        self._n += 1
        ref = f"pay_sim_{self._n:05d}"
        # THE GATEWAY STILL DOES NOT DECIDE. Same rule as a link: whether the
        # charge succeeds is the customer model's business, handed in as a
        # callback. A gateway that decided outcomes would quietly become the
        # simulation.
        ok = bool(self.mandate_outcome(reference, attempt_no)
                  if self.mandate_outcome else False)
        self.charges.append({"reference": reference, "attempt_no": attempt_no,
                             "amount_paise": amount_paise, "paid": ok,
                             "at": self.clock})
        return RecoveryArtefact(
            gateway=self.name, reference=ref, url=None,
            amount_paise=amount_paise, mode="SIMULATED",
            status="paid" if ok else "failed",
            amount_paid_paise=amount_paise if ok else 0,
            detail=("captured against the stored mandate" if ok
                    else "declined by the issuer"))

    # ------------------------------------------------------- simulation only
    def mark_paid(self, reference: str, when: datetime,
                  amount_paise: int | None = None) -> None:
        """The customer paid. Called by the customer model, never from here."""
        l = self.links[reference]
        if l.status == "paid":
            return
        l.status = "paid"
        l.paid_at = when
        l.amount_paid_paise = amount_paise if amount_paise is not None else l.amount_paise

    def open_links(self) -> list[SimLink]:
        return [l for l in self.links.values() if l.status == "created"]


def _now_from(notes: dict | None) -> datetime:
    """Creation time, taken from the caller rather than the wall clock.

    The batch runs on a simulated clock that may be days ahead of or behind
    real time. A gateway that stamped `datetime.now()` would date every link to
    the moment the script ran, and the audit trail would show links created
    after the recoveries they produced -- the same causality break that
    `--as-of` was banned from the live ledger for.

    The runner sets `gw.clock` each tick, which is the normal path;
    `notes["at"]` is the fallback for a caller that has a time but no runner.
    """
    at = (notes or {}).get("at")
    if isinstance(at, datetime):
        return at
    return datetime.fromisoformat(at) if isinstance(at, str) else datetime.min
