"""Whether a contacted customer pays, leaves, or does nothing.

THIS FILE IS THE ASSUMPTION. Everything else in the batch is real machinery;
this is the one place where a number is invented, and every number in it is a
named constant so a reviewer can change one and re-run rather than take the
result on trust.

IT REUSES THE FITTED MODEL, it does not invent a second one.
`scripts/recovery_curve.py` already fits a single free parameter -- the
per-contact conversion rate among recoverable items -- against a published
one-attempt band, and derives per-class ceilings and responsiveness from it.
Writing fresh probabilities here would have produced a batch whose recovery
rate silently disagreed with the analysis in docs/RECOVERY_RATE.md, and the two
would have had to be reconciled by hand forever. So the six failure classes are
imported. Only the two classes that analysis never covered are added below.

WHAT THE MODEL DOES NOT KNOW. It sees the decline class, the attempt number,
the channel and the amount. It does not see the schedule, the ceilings, or
whether the engine is about to stop -- for the same reason the language model
in `src/ai/` is never shown the ladder. A customer who could see the stopping
rules would let the simulation grade its own homework.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from scripts.recovery_curve import RECOVERABLE as _FAILURE_CEILING
from scripts.recovery_curve import RESPONSIVENESS as _FAILURE_RESPONSE

# --------------------------------------------------------------------------
# The two classes the recovery-curve analysis never modelled, because it was
# about failed payments and these are not failures.
#
# ABANDONED. docs/RECOVERY_RATE.md collects the published bands: 3-8% typical
# for abandoned-cart recovery, 10-14% for leaders, 15-22% for the best
# operators in the world. Nothing failed and nobody is owed anything -- the
# customer simply did not buy. The ceiling below is deliberately the least
# generous assumption in this file, because an abandoned cart is the stream
# where an optimistic number is most tempting and least defensible.
#
# OVERDUE. The opposite case: a debt that is genuinely owed, against an invoice
# the customer expects. Collection on receivables inside 30 days past due is
# high, and the failure mode is disputes and cashflow rather than disinterest.
# --------------------------------------------------------------------------
CEILING = dict(_FAILURE_CEILING, ABANDONED=0.22, OVERDUE=0.86)
RESPONSE = dict(_FAILURE_RESPONSE, ABANDONED=0.65, OVERDUE=1.15)

# Per-contact chance the customer asks to never be contacted again. Higher for
# an abandoned cart than an overdue invoice, and that ordering is the point: a
# nudge about something they chose not to buy is the message most likely to be
# received as spam. Rises with attempt number, because the fifth message is
# more annoying than the first.
OPT_OUT_BASE = {"ABANDONED": 0.055, "OVERDUE": 0.012}
OPT_OUT_DEFAULT = 0.025
OPT_OUT_FATIGUE = 0.35            # added fraction per prior contact

# Chance an overdue debtor answers with a date instead of a payment. Only
# receivables negotiate; nobody promises to pay an abandoned cart later.
PROMISE_RATE = {"OVERDUE": 0.14}
PROMISE_KEPT = 0.62               # they said Friday; do they actually pay
PROMISE_DAYS = (3, 12)

# How long after receiving a link a converting customer actually pays. Nobody
# pays instantly and nobody takes a month; a link paid four hours later and one
# paid two days later are both ordinary. Exponential with a mean in hours,
# truncated, so the reconcile pass has something to discover on a later tick
# rather than everything resolving inside the sending tick.
PAY_LAG_MEAN_HOURS = 19.0
PAY_LAG_MAX_HOURS = 96.0

# Big-ticket items convert worse on a self-serve link: a Rs 90,000 invoice
# needs an approval that a Rs 900 one does not. A gentle log-scaled penalty
# above the pivot, not a cliff.
AMOUNT_PIVOT_PAISE = 2_000_000    # Rs 20,000
AMOUNT_PENALTY = 0.11             # per e-fold above the pivot

PAYS, OPTS_OUT, PROMISES, IGNORES = "pays", "opts_out", "promises", "ignores"


@dataclass
class Persona:
    """The latent facts about one customer, fixed before any contact.

    Drawn once at book generation, never redrawn. A customer whose card is
    genuinely dead must stay dead however many times they are contacted --
    redrawing recoverability per attempt would let a long ladder recover cases
    that no ladder can, which is exactly the error the per-class ceiling exists
    to prevent.
    """
    reference: str
    decline_class: str
    recoverable: bool
    will_keep_promise: bool
    contacts_seen: int = 0
    promised: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class Reaction:
    """What the customer did about one contact."""
    kind: str                       # pays | opts_out | promises | ignores
    at: datetime | None = None      # when they pay, or the date they promise
    detail: str = ""


def draw_persona(reference: str, decline_class: str, rng) -> Persona:
    ceiling = CEILING.get(decline_class, 0.5)
    return Persona(
        reference=reference, decline_class=decline_class,
        recoverable=bool(rng.random() < ceiling),
        will_keep_promise=bool(rng.random() < PROMISE_KEPT))


def p_convert(p_base: float, persona: Persona, amount_paise: int) -> float:
    """Chance this contact converts, before the coin is flipped.

    Separated from `react` so a test can assert the shape of the curve without
    fighting a random number generator, and so the report can print the mean
    conversion probability it actually used rather than the one in a docstring.
    """
    if not persona.recoverable:
        return 0.0
    p = p_base * RESPONSE.get(persona.decline_class, 1.0)
    if amount_paise > AMOUNT_PIVOT_PAISE:
        efolds = math.log(amount_paise / AMOUNT_PIVOT_PAISE)
        p *= max(0.25, 1.0 - AMOUNT_PENALTY * efolds)
    return min(p, 0.95)


def mandate_charge(persona: Persona, amount_paise: int, p_base: float,
                   rng) -> bool:
    """A silent re-present against a standing mandate. Did it capture?

    NOT A CONTACT, and the differences are the entire value of a mandate. No
    message is sent, so there is no opt-out hazard and no fatigue: the fifth
    silent retry annoys the customer exactly as much as the first, which is to
    say not at all. What it shares with a contact is the conversion rate -- the
    money either arrived in the account or it did not, and that is the same
    question `p_convert` already answers.

    This is also precisely how `scripts/recovery_curve.py` models the silent
    series that produces Book B's 68.4%, so the batch and the analysis stay in
    agreement rather than each having their own idea of what a retry is worth.
    """
    return bool(rng.random() < p_convert(p_base, persona, amount_paise))


def react(persona: Persona, now: datetime, amount_paise: int,
          p_base: float, rng, allow_promise: bool = True) -> Reaction:
    """One contact lands. What happens.

    Order matters and is not arbitrary. Opting out is checked FIRST, because a
    customer irritated enough to leave does not first pay and then leave; the
    reverse order would quietly suppress the opt-out rate on exactly the cases
    that convert well.
    """
    persona.contacts_seen += 1
    cls = persona.decline_class

    base = OPT_OUT_BASE.get(cls, OPT_OUT_DEFAULT)
    p_out = base * (1 + OPT_OUT_FATIGUE * (persona.contacts_seen - 1))
    if rng.random() < p_out:
        return Reaction(OPTS_OUT, detail=f"asked to stop after "
                                         f"{persona.contacts_seen} contact(s)")

    if rng.random() < p_convert(p_base, persona, amount_paise):
        lag = min(rng.exponential(PAY_LAG_MEAN_HOURS), PAY_LAG_MAX_HOURS)
        return Reaction(PAYS, at=now + timedelta(hours=float(lag)),
                        detail=f"paid {lag:.0f}h after the contact")

    # A promise is only offered by someone who could have paid -- a customer
    # whose instrument is genuinely dead has nothing to promise. Without this
    # guard the ladder would pause on cases it can never recover, which
    # flatters the contact count by suppressing messages that were never going
    # to work.
    if (allow_promise and not persona.promised and persona.recoverable
            and rng.random() < PROMISE_RATE.get(cls, 0.0)):
        persona.promised = True
        days = int(rng.integers(*PROMISE_DAYS))
        return Reaction(PROMISES, at=now + timedelta(days=days),
                        detail=f"asked for {days} more days")

    return Reaction(IGNORES)
