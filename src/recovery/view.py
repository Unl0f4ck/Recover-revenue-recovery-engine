"""Campaign state, derived from the ledger. The console's only source of truth.

The product changed shape. It used to be

    failed payment -> one recovery action

and it is now

    failed payment -> persistent campaign -> wait / retry / contact
                      -> reconcile -> stop

A console that renders the first shape undersells the second, so this module
exists to turn an append-only event log into the thing a person actually needs
to see: what state is each campaign in, what happens next, and which safety
invariant is currently holding it back.

It lives in `src/` rather than in the UI builder because it is where the state
machine is INTERPRETED, and an interpretation that only exists inside a
rendering script cannot be tested. Everything here is a pure function of ledger
events plus a clock.

ONE DISTINCTION MATTERS MORE THAN THE REST. `DELIVERED` is not `RECOVERED`. A
recovery link that was created and sent is a contact that happened; whether the
customer paid is unknown until the API is read. A console that shows a sent
link as a success is reporting a send rate and calling it a recovery rate,
which is the single easiest way for this kind of system to flatter itself. The
two states are kept apart here, and the path between them --

    DELIVERED -> awaiting payment -> reconciled from Razorpay -> RECOVERED

-- is rendered explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import dunning as D
from . import ledger as L
from .campaign import rebuild
from .declines import describe, load_declines

# Live states.
WAITING = "WAITING"                      # opened, first attempt not yet due
DUE = "DUE"                              # a step is due right now
QUIET_HOLD = "QUIET_HOLD"                # contact deferred out of quiet hours
DELIVERED = "DELIVERED"                  # contact made, money not yet moved
RETRY_SCHEDULED = "RETRY_SCHEDULED"      # attempted, next attempt dated

# Terminal states.
RECOVERED = "RECOVERED"
ESCALATED = "ESCALATED"                  # handed to a human
WRITTEN_OFF = "WRITTEN_OFF"
EXHAUSTED = "EXHAUSTED"
CLOSED_TERMINAL = "CLOSED_TERMINAL"      # the payment itself resolved elsewhere
HALTED = "HALTED"
OPTED_OUT = "OPTED_OUT"           # the customer asked us to stop

TERMINAL = {RECOVERED, ESCALATED, WRITTEN_OFF, EXHAUSTED, CLOSED_TERMINAL,
            HALTED, OPTED_OUT}

# Stop reasons -> the state a person would name.
_STOP_STATE = {
    D.STOP_RECOVERED: RECOVERED,
    D.STOP_EXHAUSTED: EXHAUSTED,
    D.STOP_EXPIRED: EXHAUSTED,
    D.STOP_TERMINAL_STATE: CLOSED_TERMINAL,
    D.STOP_CONTACT_CEILING: EXHAUSTED,
    D.STOP_KILL_SWITCH: HALTED,
    D.STOP_OPTED_OUT: OPTED_OUT,
    # Closed because the money is counted elsewhere, not because recovery
    # failed. Rendered as a resolution rather than a loss.
    "superseded_by_invoice": CLOSED_TERMINAL,
    "human_review_required": ESCALATED,
}

_ORDER = [DUE, QUIET_HOLD, DELIVERED, RETRY_SCHEDULED, WAITING,
          RECOVERED, ESCALATED, OPTED_OUT, WRITTEN_OFF, EXHAUSTED,
          CLOSED_TERMINAL, HALTED]


@dataclass
class TimelineEntry:
    at: datetime
    label: str
    detail: str = ""
    kind: str = "past"                   # past | now | future
    tone: str = ""                       # ok | hold | win | stop


@dataclass
class Invariants:
    """The safety properties, made visible rather than merely enforced.

    A reviewer should not have to take on trust that the sequencer is bounded.
    Each of these is read straight off the ledger for this one reference.
    """
    attempt: str                         # "2 of 4"
    retryable: bool
    decline_class: str
    classification: str
    next_action: str | None
    next_due: datetime | None
    quiet_hold_until: datetime | None
    idempotency_key: str | None
    last_reconciliation: str | None
    stop_reason: str | None
    execution_mode: str                  # REAL | SIMULATED | -
    already_succeeded: bool
    contacts_used: str                   # "1 of 3"


@dataclass
class Campaign:
    reference: str
    sequence_id: str
    kind: str
    state: str
    at_risk_paise: int
    recovered_paise: int
    opened_at: datetime
    failed_at: datetime | None
    schedule: str
    segment: str | None
    timeline: list[TimelineEntry] = field(default_factory=list)
    invariants: Invariants | None = None
    steps: list[dict] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


# ---------------------------------------------------------------------------

def _quiet_hold(events: list[dict], now: datetime) -> datetime | None:
    """An active quiet-hours deferral, if the deferred step has not since run."""
    holds = [e for e in events if e["event"] == L.CONTACT_DEFERRED]
    if not holds:
        return None
    last = holds[-1]
    until = (last.get("extra") or {}).get("defer_until")
    if not until:
        return None
    until_dt = datetime.fromisoformat(until)
    if until_dt <= now:
        return None
    # A later attempt on this reference means the hold was released.
    ran = [e for e in events if e["event"] == L.ATTEMPT_INFLIGHT
           and datetime.fromisoformat(e["at"]) > datetime.fromisoformat(last["at"])]
    return None if ran else until_dt


def state_of(seq: D.Sequence, events: list[dict], now: datetime,
             cfg: dict, path: Path | None) -> str:
    """One campaign's state. Terminal reasons win; then holds; then progress."""
    stopped = [e for e in events if e["event"] == L.SEQUENCE_STOPPED]
    if stopped:
        reason = (stopped[-1].get("extra") or {}).get("stop_reason", "")
        if reason == D.STOP_TERMINAL_CHANNEL:
            n = min(D.current_attempt_no(seq.reference, path),
                    len(seq.steps) - 1)
            ch = seq.steps[n].channel if seq.steps else ""
            return WRITTEN_OFF if ch == "write_off" else ESCALATED
        return _STOP_STATE.get(reason, EXHAUSTED)

    if L.has_succeeded(seq.reference, path):
        return RECOVERED
    if _quiet_hold(events, now):
        return QUIET_HOLD

    n = D.current_attempt_no(seq.reference, path)
    if D.due_step(seq, now, cfg, path) is not None:
        return DUE
    if n == 0:
        return WAITING

    # An attempt landed. If the most recent decided outcome was a delivered
    # contact, that is the informative state -- the customer has a link and we
    # are waiting on them, which is materially different from "a retry is
    # scheduled" even though both are true at once.
    decided = [e for e in events if e["event"] in
               (L.ATTEMPT_DELIVERED, L.ATTEMPT_FAILED, L.ATTEMPT_SUCCEEDED)]
    if decided and decided[-1]["event"] == L.ATTEMPT_DELIVERED:
        return DELIVERED
    return RETRY_SCHEDULED if n < len(seq.steps) else EXHAUSTED


# ---------------------------------------------------------------------------

# PLAIN LANGUAGE, on purpose.
#
# The engine's own vocabulary is precise and means nothing to someone reading
# this for the first time: CUSTOMER_ABORTED, single_nudge, "not retryable,
# schedule 'slow'". Those names stay in the ledger, which is the record of
# record; the timeline a person reads says what happened in the words they
# would use. Precision is not lost -- it is just not the timeline's job.
_LABEL = {
    "silent": "Retried quietly in the background",
    "payment_link": "Sent them a payment link",
    "alternate_method_link": "Sent a link with a different payment option",
    "payment_update": "Asked them to update their card",
    "reconcile": "Checked with Razorpay",
    "human_review": "Handed to a person",
    "write_off": "Gave up on this one",
}

_NEXT = {
    "silent": "Retry quietly",
    "payment_link": "Send a payment link",
    "alternate_method_link": "Send a different payment option",
    "payment_update": "Ask them to update their card",
    "reconcile": "Check with Razorpay",
    "human_review": "Hand to a person",
    "write_off": "Give up",
}

# What actually went wrong, said out loud.
_CAUSE = {
    "ABANDONED": "They left the checkout without paying",
    "CUSTOMER_ABORTED": "They cancelled the payment themselves",
    "SOFT_FUNDS": "Not enough money in the account at the time",
    "SOFT_TECHNICAL": "A bank or payment gateway was down",
    "AUTH": "The one-time password failed",
    "VPA": "The UPI address was wrong or blocked",
    "HARD_INSTRUMENT": "The card is dead - expired, blocked or invalid",
    "UNKNOWN": "The reason was unclear, so we checked with Razorpay first",
}

# Why that leads to this plan.
_PLAN = {
    "ABANDONED": "nudge them a few times, then stop",
    "CUSTOMER_ABORTED": "one polite nudge only - they already said no",
    "SOFT_FUNDS": "wait for payday and ask again",
    "SOFT_TECHNICAL": "try again in a few hours, on a different route",
    "AUTH": "bring them back to pay, since we cannot do it for them",
    "VPA": "ask for a different payment method",
    "HARD_INSTRUMENT": "never retry a dead card; ask them to use another",
    "UNKNOWN": "check with Razorpay before doing anything",
}


# Razorpay's own reason codes, said out loud. Anything unmapped falls back to
# the code with its underscores removed -- readable, and honest that we are
# passing the bank's own word through rather than inventing one.
_REASON = {
    "payment_cancelled": "the customer cancelled it",
    "insufficient_funds": "there was not enough money in the account",
    "card_expired": "the card had expired",
    "debit_instrument_blocked": "the card was blocked",
    "card_number_invalid": "the card number was not valid",
    "international_transaction_not_allowed": "international payments were off",
    "bank_technical_error": "the bank had a technical problem",
    "bank_not_available": "the bank was unreachable",
    "issuer_technical_error": "the card issuer had a problem",
    "bank_cutoff_in_progress": "the bank was in its nightly cutoff",
    "gateway_technical_error": "the payment gateway had a problem",
    "payment_timed_out": "the payment timed out",
    "upi_app_technical_error": "the UPI app had a problem",
    "psp_app_not_available": "the UPI app was unavailable",
    "incorrect_otp": "the one-time password was wrong",
    "otp_expired": "the one-time password expired",
    "otp_attempts_exceeded": "too many one-time password attempts",
    "authentication_failed": "the bank could not verify them",
    "invalid_vpa": "the UPI address was not valid",
    "transaction_on_vpa_restricted": "that UPI address is restricted",
    "transaction_limit_exceeded": "it went over their transaction limit",
    "transaction_daily_limit_exceeded": "it went over their daily limit",
    "credit_limit_exceeded": "it went over their credit limit",
    "payment_failed": "no specific reason given",
    "card_declined": "the card was declined without a reason",
}


def _plain_reason(reason: str | None) -> str:
    if not reason:
        return "no reason given"
    return _REASON.get(reason, reason.replace("_", " "))


def _future_label(channel: str) -> str:
    """What a step WILL be, said the way a person would say it."""
    return _NEXT.get(channel, channel)


def _plain_cause(cls: str) -> str:
    return _CAUSE.get(cls, cls.replace("_", " ").lower())


def _plain_plan(cls: str, n_steps: int, days: float) -> str:
    how = _PLAN.get(cls, "")
    span = f"{n_steps} step{'s' if n_steps != 1 else ''} over {days:.0f} days"
    return f"{span} - {how}" if how else span



_STOP_SAY = {
    "recovered": "they paid",
    "attempts_exhausted": "we ran out of attempts",
    "sequence_age_limit": "it had been too long",
    "terminal_payment_state_observed": "it got settled another way",
    "terminal_channel_reached": "the plan reached its end",
    "contact_ceiling_reached": "we had messaged them enough",
    "customer_opted_out": "they asked us to stop",
    "kill_switch": "an operator stopped everything",
    "superseded_by_invoice": "the same money is tracked as an invoice",
}

def timeline_of(seq: D.Sequence, events: list[dict], now: datetime,
                cfg: dict, path: Path | None) -> list[TimelineEntry]:
    """The whole ladder: what happened, and what is still scheduled.

    Showing only the latest action is what made the old console undersell the
    system. A campaign is a plan, and a plan is only legible if you can see the
    part that has not run yet alongside the part that has.
    """
    # PAST entries are kept in LEDGER ORDER, not sorted by timestamp.
    #
    # The ledger is append-only, so the order events were written IS the order
    # they happened -- that is the causal record. The `at` field is what the
    # process believed the time was, and a process handed a shifted clock can
    # record a timestamp that reorders the story: a recovery appearing before
    # the link that produced it. Sorting by `at` would render that reordering as
    # fact. Sorting by position renders what happened, and shows the odd
    # timestamp beside it rather than reshuffling around it.
    out: list[TimelineEntry] = []
    opened = next((e for e in events if e["event"] == L.SEQUENCE_OPENED), None)
    extra = (opened or {}).get("extra") or {}

    if extra.get("failed_at"):
        # Three streams, three different things that went wrong, and calling
        # them all "Payment failed" reads as a bug to anyone looking at an
        # invoice: nothing failed, a due date passed. The audit trail is read
        # by people deciding whether to trust it.
        title, why = {
            "checkout_abandoned": ("Money at risk", "customer left the checkout"),
            "overdue_receivable": ("Invoice fell due", "not paid by the due date"),
        }.get(seq.kind,
              ("Payment failed", _plain_reason(extra.get("reason"))))
        out.append(TimelineEntry(
            datetime.fromisoformat(extra["failed_at"]), title, why))

    cls = seq.classification.decline_class
    span_days = ((seq.steps[-1].due_at - seq.opened_at).total_seconds() / 86400
                 if seq.steps else 0)
    out.append(TimelineEntry(
        seq.opened_at, "Worked out why", _plain_cause(cls)))
    out.append(TimelineEntry(
        seq.opened_at, "Made a plan",
        _plain_plan(cls, len(seq.steps), span_days)))

    for e in events:
        at = datetime.fromisoformat(e["at"])
        ev, ch = e["event"], e.get("channel") or ""
        if ev == L.CONTACT_DEFERRED:
            until = (e.get("extra") or {}).get("defer_until", "")
            out.append(TimelineEntry(
                at, "Too late at night to message",
                f"waiting until {until[11:16]}" if until else e["detail"],
                tone="hold"))
        elif ev == L.ATTEMPT_DELIVERED:
            out.append(TimelineEntry(
                at, _LABEL.get(ch, ch), "waiting for them to pay", tone="ok"))
        elif ev == L.ATTEMPT_UNEXECUTABLE:
            out.append(TimelineEntry(
                at, "Could not retry quietly",
                "this account has no saved card to charge", tone="hold"))
        elif ev == L.ATTEMPT_AMBIGUOUS:
            out.append(TimelineEntry(
                at, "Lost connection - will retry the same step",
                "the same attempt, so they cannot be charged twice",
                tone="hold"))
        elif ev == L.ATTEMPT_FAILED:
            out.append(TimelineEntry(at, "That did not work",
                                     "moving to the next step"))
        elif ev == L.ATTEMPT_SUCCEEDED:
            out.append(TimelineEntry(
                at, "They paid",
                "confirmed by Razorpay, not assumed", tone="win"))
        elif ev == L.RECONCILED:
            out.append(TimelineEntry(at, "Checked with Razorpay", e["detail"]))
        elif ev == L.SEQUENCE_STOPPED:
            why = (e.get("extra") or {}).get("stop_reason", "")
            out.append(TimelineEntry(
                at, "Closed", _STOP_SAY.get(why, why.replace("_", " ")),
                tone="stop"))

    if not any(e["event"] == L.SEQUENCE_STOPPED for e in events):
        n = D.current_attempt_no(seq.reference, path)
        for s in seq.steps[n:]:
            out.append(TimelineEntry(
                s.due_at, _future_label(s.channel),
                f"step {s.attempt_no + 1} of {len(seq.steps)}"
                + (" - needs a saved card we do not have"
                   if s.requires_mandate else ""),
                kind="future"))
    return out


def invariants_of(seq: D.Sequence, events: list[dict], now: datetime,
                  cfg: dict, path: Path | None) -> Invariants:
    n = D.current_attempt_no(seq.reference, path)
    stopped = [e for e in events if e["event"] == L.SEQUENCE_STOPPED]
    nxt = seq.steps[n] if n < len(seq.steps) and not stopped else None
    delivered = [e for e in events if e["event"] == L.ATTEMPT_DELIVERED]
    # A success recorded by `campaign.reconcile_links` IS a reconciliation -- it
    # is the API read that promoted a delivered link to recovered. Looking only
    # for RECONCILED events made a campaign that had just been reconciled report
    # "link status not yet read back".
    recon = [e for e in events
             if e["event"] in (L.RECONCILED, L.ATTEMPT_SUCCEEDED)]
    contacting = cfg["compliance"]["contacting_channels"]

    return Invariants(
        attempt=f"{min(n + 1, len(seq.steps))} of {len(seq.steps)}",
        retryable=seq.classification.retryable,
        decline_class=seq.classification.decline_class,
        classification=describe(seq.classification),
        next_action=_future_label(nxt.channel) if nxt else None,
        next_due=nxt.due_at if nxt else None,
        quiet_hold_until=_quiet_hold(events, now),
        idempotency_key=(L.idempotency_key(seq.reference, n, seq.policy_version)
                         if nxt else None),
        last_reconciliation=(recon[-1]["detail"] if recon else
                             ("link status not yet read back" if delivered
                              else None)),
        stop_reason=((stopped[-1].get("extra") or {}).get("stop_reason")
                     if stopped else None),
        execution_mode=(delivered[-1].get("execution_mode", "SIMULATED")
                        if delivered else "-"),
        already_succeeded=L.has_succeeded(seq.reference, path),
        contacts_used=(f"{L.contacts_made(seq.reference, contacting, path)} of "
                       f"{cfg['compliance']['max_contacts_per_reference']}"),
    )


# ---------------------------------------------------------------------------

def campaigns(now: datetime, cfg: dict | None = None,
              path: Path | None = None) -> list[Campaign]:
    cfg = cfg or load_declines()
    rows = L.read(path)
    refs: list[str] = []
    for e in rows:
        if e["event"] == L.SEQUENCE_OPENED and e["reference"] not in refs:
            refs.append(e["reference"])

    out: list[Campaign] = []
    for ref in refs:
        seq = rebuild(ref, cfg, path)
        if seq is None:
            continue
        events = L.events_for(ref, path)
        opened = next(e for e in events if e["event"] == L.SEQUENCE_OPENED)
        extra = opened.get("extra") or {}
        won = sum(int(e.get("amount_paise", 0)) for e in events
                  if e["event"] == L.ATTEMPT_SUCCEEDED)
        out.append(Campaign(
            reference=ref, sequence_id=seq.sequence_id, kind=seq.kind,
            state=state_of(seq, events, now, cfg, path),
            at_risk_paise=seq.at_risk_paise, recovered_paise=won,
            opened_at=seq.opened_at,
            failed_at=(datetime.fromisoformat(extra["failed_at"])
                       if extra.get("failed_at") else None),
            schedule=seq.classification.schedule,
            segment=seq.failing_method,
            timeline=timeline_of(seq, events, now, cfg, path),
            invariants=invariants_of(seq, events, now, cfg, path),
            steps=[{"attempt_no": s.attempt_no, "channel": s.channel,
                    "rail": s.rail, "due_at": s.due_at.isoformat(),
                    "requires_mandate": s.requires_mandate,
                    "done": s.attempt_no < D.current_attempt_no(ref, path)}
                   for s in seq.steps],
        ))
    out.sort(key=lambda c: (-c.at_risk_paise,))
    return out


def state_counts(cs: list[Campaign]) -> list[tuple[str, int]]:
    counts = {s: 0 for s in _ORDER}
    for c in cs:
        counts[c.state] = counts.get(c.state, 0) + 1
    return [(s, counts[s]) for s in _ORDER if counts.get(s)]


def funnel(cs: list[Campaign], path: Path | None = None) -> list[tuple[str, int, str]]:
    """The sequencer's own funnel. Every row is counted from the ledger.

    `silent retries executed` is reported even though it is zero, because zero
    is the finding: this account holds no saved token or e-mandate, so the
    cheapest and least intrusive rung of the ladder cannot run at all. Hiding a
    zero that explains the recovery ceiling would be the wrong kind of tidy.
    """
    rows = L.read(path)
    ev = lambda k: sum(1 for e in rows if e["event"] == k)   # noqa: E731
    return [
        ("Sequences opened", ev(L.SEQUENCE_OPENED), "one per unit of revenue at risk"),
        ("Recovery actions executed", ev(L.ATTEMPT_INFLIGHT),
         "written ahead, with an idempotency key, before any call"),
        ("Links delivered", ev(L.ATTEMPT_DELIVERED), "contact made — not yet money"),
        ("Silent retries executed", 0,
         "needs a saved token or e-mandate; this account has neither"),
        ("Deferred by quiet hours", ev(L.CONTACT_DEFERRED), "held, not dropped"),
        ("Reconciled from the API", ev(L.RECONCILED) + ev(L.ATTEMPT_SUCCEEDED),
         "the only route from delivered to recovered"),
        ("Recovered", ev(L.ATTEMPT_SUCCEEDED), "measured, read back from Razorpay"),
        ("Escalated to a human", sum(1 for c in cs if c.state == ESCALATED),
         "handed over rather than retried again"),
        ("Reached a terminal state", sum(1 for c in cs if c.terminal),
         f"{sum(1 for c in cs if not c.terminal)} still running"),
    ]
