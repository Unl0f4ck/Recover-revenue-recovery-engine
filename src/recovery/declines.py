"""Decline classification. Is this failure retryable at all?

Deliberately separate from the scheduler in `dunning.py`. Kill Bill's
payment-retries plugin makes the same split, and the reason is testability:
"is `card_expired` retryable" is a question with one right answer that never
changes, while "when should attempt 3 fire" depends on a clock, a config and
what has already happened. Tangle them and neither can be checked in isolation.

Nothing here has a clock, a network call, or any state. Given a Razorpay error
reason it returns a class; that is all it does.

THE UNMAPPED-REASON RULE. A reason string this table has never seen classifies
as UNKNOWN, which is `retryable: false` and schedules a reconciliation before
anything else. That is the whole safety property of this module: Razorpay can
add an error code tomorrow and the worst we do is ask the API what happened.
The alternative default -- treating unrecognised failures as soft and retrying
them -- is how a dunning system charges a customer whose card was reported
stolen under a code we had not mapped yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..yamlcache import load_yaml

CONFIG = Path(__file__).resolve().parents[2] / "config"


def load_declines() -> dict:
    return load_yaml(CONFIG / "declines.yaml")


@dataclass(frozen=True)
class Classification:
    """What we know about a failure before deciding anything."""
    decline_class: str          # HARD_INSTRUMENT | SOFT_FUNDS | ... | UNKNOWN
    retryable: bool
    schedule: str               # key into declines.yaml `schedules`
    reason: str | None          # the raw Razorpay reason, carried through
    mapped: bool                # False => reason string not in the table

    @property
    def needs_reconciliation(self) -> bool:
        return self.schedule == "reconcile_first"


def _index(cfg: dict) -> dict[str, str]:
    """reason -> class name. Built fresh each call; the table is ~30 entries and
    a module-level cache would go stale against an edited config during a run.
    """
    out: dict[str, str] = {}
    for name, spec in cfg["classes"].items():
        for r in spec.get("reasons", []) or []:
            out[r] = name
    return out


def classify(reason: str | None, cfg: dict | None = None) -> Classification:
    """Razorpay error `reason` -> Classification.

    `reason` is the field Razorpay returns on a failed payment alongside
    `source` and `step` (see config/signatures.yaml, which uses the same
    vocabulary for a different purpose -- that file identifies which *rail* is
    degrading, this one identifies whether *this payment* can be retried).
    """
    cfg = cfg or load_declines()
    idx = _index(cfg)
    name = idx.get(reason or "")
    mapped = name is not None
    if not mapped:
        name = "UNKNOWN"
    spec = cfg["classes"][name]
    return Classification(
        decline_class=name,
        retryable=bool(spec.get("retryable", False)),
        schedule=str(spec["schedule"]),
        reason=reason,
        mapped=mapped,
    )


def classify_abandonment(cfg: dict | None = None) -> Classification:
    """An abandoned checkout is not a decline and has no reason string.

    Given its own constructor rather than being forced through `classify`,
    because passing None and getting UNKNOWN would be wrong in a way that
    reads as right: an abandonment needs no reconciliation, nothing failed,
    and there is no ambiguity to resolve with the gateway.
    """
    cfg = cfg or load_declines()
    return Classification(decline_class="ABANDONED", retryable=False,
                          schedule="abandoned", reason=None, mapped=True)


def classify_subscription(reason: str | None,
                          cfg: dict | None = None) -> Classification:
    """A failed recurring charge. Same decline, different conclusion.

    THE REASON IS CLASSIFIED EXACTLY AS A ONE-OFF FAILURE -- insufficient funds
    is insufficient funds -- so the decline class, and with it the retryability
    judgement, is unchanged. What changes is the SCHEDULE, and that is the
    whole point of the workflow: a mandate is standing consent, so the cheap
    silent rungs become available and the ladder can afford to wait for payday
    instead of spending a customer contact.

    Without this the routing was simply missing. `config/workflows.yaml`
    declared `schedule: mandate` for the subscription workflow, but nothing
    read it -- `classify_item` fell through to the ordinary reason-based
    classifier, so a failed subscription charge would have been put on the
    `slow` checkout ladder and every silent rung the workflow exists for would
    have been absent from the plan.
    """
    cfg = cfg or load_declines()
    base = classify(reason, cfg)
    return Classification(decline_class=base.decline_class,
                          retryable=base.retryable, schedule="mandate",
                          reason=base.reason, mapped=base.mapped)


def classify_receivable(cfg: dict | None = None) -> Classification:
    """An overdue invoice is not a decline either.

    Nothing failed and nothing was abandoned: the customer was invoiced, agreed
    terms, and the terms ran out. It gets its own class so the reason shown to
    an operator is the true one -- "the terms ran out" -- rather than being
    forced through a failure taxonomy that has no row for it.
    """
    cfg = cfg or load_declines()
    return Classification(decline_class="OVERDUE", retryable=False,
                          schedule="overdue", reason=None, mapped=True)


def describe(c: Classification) -> str:
    """One line an operator can read in the audit trail."""
    if c.decline_class == "ABANDONED":
        return "checkout abandoned; nothing failed, so nothing to re-present"
    if c.decline_class == "OVERDUE":
        return "invoice past its payment terms; the money is already owed"
    if not c.mapped:
        return (f"reason {c.reason!r} is not in the decline table; "
                f"reconciling with the gateway before any action")
    verb = "retryable" if c.retryable else "not retryable"
    return f"{c.reason} -> {c.decline_class} ({verb}, schedule '{c.schedule}')"
