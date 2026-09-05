"""The gateway contract.

Small on purpose. This is what a recovery sequencer needs from a payment
provider, not a general payments abstraction -- there is no charge(), no
refund(), no subscription management, because this system does none of those
things and an interface that promises them would be a lie about our scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol


@dataclass(frozen=True)
class Capabilities:
    """What a provider can actually do, as data rather than as folklore.

    Every honesty caveat in this project is a capability gap. Payment Links
    cannot restrict which method a customer uses; this account holds no saved
    mandate, so a silent re-present is impossible. Both were true and both were
    recorded in prose and enforced by hand-written conditionals. Here they are
    values the sequencer can read, which is what stops a Razorpay limitation
    being hard-coded as a universal truth about payments.
    """
    name: str
    # Can a recovery artefact be restricted to specific payment methods?
    can_restrict_methods: bool = False
    # Can the provider notify the customer itself (SMS / email on a link)?
    can_notify: bool = False
    # Can we charge a stored instrument with no customer present?
    can_charge_saved_instrument: bool = False
    # Does the provider sign its webhooks, and how?
    webhook_signature: str | None = None
    # Ceiling the provider imposes on recovery artefacts, if any.
    max_recovery_artefacts: int | None = None
    notes: str = ""

    def explain(self) -> list[str]:
        """The gaps, in the words an operator would use. Rendered in the console
        so a reviewer sees what the system cannot do without reading source."""
        out = []
        if not self.can_restrict_methods:
            out.append("cannot restrict which payment method a customer uses "
                       "on a recovery link")
        if not self.can_charge_saved_instrument:
            out.append("cannot re-present a payment without the customer "
                       "(no saved token or mandate)")
        if not self.can_notify:
            out.append("cannot deliver the recovery link itself")
        return out


@dataclass(frozen=True)
class RecoveryArtefact:
    """Something payable, put in front of a customer."""
    gateway: str
    reference: str | None            # provider id, when real
    url: str | None
    amount_paise: int
    mode: str = "SIMULATED"          # REAL | SIMULATED
    status: str = "created"
    amount_paid_paise: int = 0
    method_restriction_enforced: bool = False
    notified: dict = field(default_factory=dict)   # channel -> requested?
    detail: str = ""

    @property
    def paid(self) -> bool:
        return self.status in ("paid", "captured")


@dataclass(frozen=True)
class WebhookEvent:
    """A verified provider event, normalised."""
    gateway: str
    event_id: str
    event: str                       # provider-native event name
    kind: str                        # normalised: recovery_paid | payment_failed | ...
    at: datetime
    reference: str | None            # the object it concerns
    amount_paise: int = 0
    payload: dict = field(default_factory=dict)


class Gateway(Protocol):
    """What every adapter must provide."""

    def capabilities(self) -> Capabilities: ...

    def create_recovery(self, amount_paise: int, description: str,
                        exclude_methods: list[str] | None = None,
                        notify: dict | None = None,
                        customer: dict | None = None,
                        notes: dict | None = None,
                        dry_run: bool = False) -> RecoveryArtefact: ...

    def fetch_recovery(self, reference: str) -> RecoveryArtefact: ...

    def charge_mandate(self, amount_paise: int, reference: str,
                       attempt_no: int = 0) -> RecoveryArtefact:
        """Re-present against a stored mandate. Nobody is contacted.

        Only meaningful when `capabilities().can_charge_saved_instrument` is
        true; callers must check first. It is on the protocol rather than
        bolted onto one adapter because the whole point of `Capabilities` is
        that a provider's limits are DATA -- an adapter that cannot do this
        says so in its capabilities and raises here, and the sequencer reads
        the capability instead of assuming every account is like this one.
        """
        raise NotImplementedError

    def verify_webhook(self, raw_body: bytes, headers: dict,
                       secret: str, now: datetime | None = None
                       ) -> WebhookEvent: ...


_REGISTRY: dict[str, Callable[..., Gateway]] = {}


def register(name: str, factory: Callable[..., Gateway]) -> None:
    _REGISTRY[name] = factory


def get_gateway(name: str = "razorpay", **kw) -> Gateway:
    if name not in _REGISTRY:
        raise KeyError(f"no gateway adapter registered for {name!r}; "
                       f"have {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kw)


def available() -> list[str]:
    return sorted(_REGISTRY)
