"""The retry sequencer. One failure becomes a scheduled campaign, not one shot.

This is the piece the system was missing. Everything before it did a single
bounded intervention per unit of revenue at risk, which caps recovery at the
success rate of one attempt -- the industry's no-automation band, 20-31%. Every
production dunning system in docs/PRIOR_ART.md instead runs a SEQUENCE over
days, and that is the entire reason comprehensive dunning reports 70-80%: the
rate compounds across attempts that are spaced far enough apart for the
underlying cause to have changed.

Spacing is the load-bearing part. Retrying a declined card thirty seconds later
fails for the same reason it failed the first time; retrying it on payday does
not. So a sequence is measured in days and its steps are chosen by WHY the
payment failed, which is `declines.py`'s job.

FOUR PROPERTIES, each with a named regression test:

  1. A transport failure re-issues the SAME attempt with the SAME idempotency
     key, and does not advance the sequence.        (medusa#16292)
  2. Every terminal state stops the sequence.       (medusa#16398)
  3. At most one success per reference, read from the ledger.   (recoup)
  4. No customer contact inside quiet hours, and never more than the compliance
     ceiling allows.                                (track brief: "compliant")

WHAT THIS MODULE DOES NOT DO. It does not re-present a card. That needs a saved
token or an e-mandate, and this account has neither (docs/PRIOR_ART.md 1.8). A
step marked `requires_mandate` is recorded as UNEXECUTABLE and the sequence
moves on -- it is not modelled as though it ran, and it never counts toward
recovered value.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ledger
from . import channels, notify, promises, suppression
from .declines import Classification, load_declines

IST = timezone(timedelta(hours=5, minutes=30))

# Stop reasons. Strings rather than an enum so they land in the JSONL ledger
# readable without a decoder ring.
STOP_RECOVERED = "recovered"
STOP_TERMINAL_STATE = "terminal_payment_state_observed"
STOP_EXHAUSTED = "attempts_exhausted"
STOP_EXPIRED = "sequence_age_limit"
STOP_TERMINAL_CHANNEL = "terminal_channel_reached"
STOP_CONTACT_CEILING = "contact_ceiling_reached"
# Distinct from the ceiling on purpose. "We have messaged this person
# enough times" and "this person asked us to stop" are different facts,
# and collapsing them loses the one an auditor actually cares about.
STOP_OPTED_OUT = "customer_opted_out"
STOP_PROMISED = "promise_to_pay_pending"   # not an ending; a pause
STOP_KILL_SWITCH = "kill_switch"


@dataclass(frozen=True)
class Step:
    attempt_no: int
    due_at: datetime
    channel: str
    rail: str                       # same | alternate | none
    requires_mandate: bool = False


@dataclass
class Sequence:
    reference: str
    sequence_id: str
    kind: str                       # payment_failure | checkout_abandoned
    at_risk_paise: int
    opened_at: datetime
    classification: Classification
    steps: list[Step]
    policy_version: str
    # From the attribution ladder, when the aggregate path produced one. Drives
    # what `rail: alternate` actually means -- see `exclude_methods`.
    diagnosis_family: str | None = None
    failing_method: str | None = None
    customer_ref: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def deadline(self) -> datetime | None:
        return None


# ---------------------------------------------------------------------------
# PLANNING
# ---------------------------------------------------------------------------

def plan(reference: str, kind: str, at_risk_paise: int,
         classification: Classification, opened_at: datetime,
         cfg: dict | None = None, diagnosis_family: str | None = None,
         failing_method: str | None = None,
         customer_ref: str | None = None) -> Sequence:
    """Turn one failure into a dated campaign.

    Every step's `due_at` is offset from the ORIGINAL failure, not from the
    previous attempt. That matters when an attempt is deferred out of quiet
    hours or blocked by a bound: the schedule does not drift later and later
    behind the customer's actual situation, which is what makes a day-7 step
    still land on day 7.
    """
    cfg = cfg or load_declines()
    spec = cfg["schedules"][classification.schedule]
    version = str(cfg.get("policy_version", "dunning-1.0"))
    cap = int(cfg["stopping"]["max_attempts"])

    steps = [
        Step(attempt_no=i,
             due_at=opened_at + timedelta(hours=float(s["after_hours"])),
             channel=str(s["channel"]),
             rail=str(s.get("rail", "none")),
             requires_mandate=bool(s.get("requires_mandate", False)))
        for i, s in enumerate(spec["steps"][:cap])
    ]
    if (kind == "payment_failure" and classification.decline_class == "SOFT_TECHNICAL"
            and diagnosis_family in ("issuer_degradation", "psp_degradation", "upi_network_degradation")):
        # A measured outage earns an early alternate-method recovery step.
        # Ordinary technical declines still use the standard six-hour ladder.
        steps[0] = Step(0, opened_at + timedelta(hours=1),
                        "alternate_method_link", "alternate")
    return Sequence(
        reference=reference, sequence_id=ledger.sequence_id(reference, version),
        kind=kind, at_risk_paise=int(at_risk_paise), opened_at=opened_at,
        classification=classification, steps=steps, policy_version=version,
        diagnosis_family=diagnosis_family, failing_method=failing_method,
        customer_ref=customer_ref)


def may_open(at_risk_paise: int, cfg: dict | None = None,
             kind: str | None = None, wf_cfg: dict | None = None
             ) -> tuple[bool, str]:
    """The exposure floor, applied before a sequence exists at all.

    THIS IS A POLICY CHOICE, NOT A BREAK-EVEN, and it used to be described as
    the latter. Messaging costs tens of paise, so the point below which chasing
    genuinely loses money is around Rs 15 -- computed per workflow in
    `economics.py`. Every floor here sits far above that on purpose, because a
    contact also spends a customer's tolerance and the merchant's sender
    reputation, and neither scales with the amount at stake.

    The floor is per WORKFLOW, because the streams differ in how welcome a
    message is: an overdue invoice is expected, an unsolicited nudge about a
    small abandoned cart is the one most likely to be marked as spam.
    """
    cfg = cfg or load_declines()
    from .economics import floor_for
    floor = (floor_for(kind, cfg, wf_cfg) if kind
             else int(cfg["stopping"]["min_at_risk_paise"]))
    if at_risk_paise < floor:
        return False, (f"below the floor we chose for this kind of case: "
                       f"Rs {at_risk_paise/100:,.0f} < Rs {floor/100:,.0f}")
    return True, ""


def open_sequence(seq: Sequence, path: Path | None = None) -> None:
    ledger.append(ledger.LedgerEvent(
        at=seq.opened_at, event=ledger.SEQUENCE_OPENED, reference=seq.reference,
        sequence_id=seq.sequence_id, attempt_no=-1,
        policy_version=seq.policy_version,
        decline_class=seq.classification.decline_class,
        amount_paise=seq.at_risk_paise,
        detail=f"{len(seq.steps)}-step '{seq.classification.schedule}' schedule",
        extra={"kind": seq.kind, "reason": seq.classification.reason,
               "mapped": seq.classification.mapped,
               "failing_method": seq.failing_method,
               "diagnosis_family": seq.diagnosis_family,
               "classification": asdict(seq.classification),
               "plan": [{**asdict(s), "due_at": s.due_at.isoformat()}
                        for s in seq.steps], **seq.extra}), path)
    for s in seq.steps:
        ledger.append(ledger.LedgerEvent(
            at=seq.opened_at, event=ledger.ATTEMPT_SCHEDULED,
            reference=seq.reference, sequence_id=seq.sequence_id,
            attempt_no=s.attempt_no, policy_version=seq.policy_version,
            channel=s.channel, rail=s.rail,
            idempotency_key=ledger.idempotency_key(
                seq.reference, s.attempt_no, seq.policy_version),
            amount_paise=seq.at_risk_paise,
            detail=f"due {s.due_at.isoformat()}"), path)


# ---------------------------------------------------------------------------
# WHERE ARE WE
# ---------------------------------------------------------------------------

def current_attempt_no(reference: str, path: Path | None = None) -> int:
    """The index of the step we are on.

    Equal to the number of DECIDED outcomes so far, which is what makes
    property (1) hold structurally rather than by remembering to special-case
    it: an ambiguous attempt leaves this number unchanged, so recomputing the
    idempotency key produces the same string and the re-issue is a genuine
    retry of the same operation rather than a second charge.
    """
    return ledger.attempts_made(reference, path)


def last_attempt_at(reference: str, path: Path | None = None) -> datetime | None:
    """When we last actually reached out, from the write-ahead records."""
    ts = [datetime.fromisoformat(e["at"]) for e in ledger.events_for(reference, path)
          if e["event"] == ledger.ATTEMPT_INFLIGHT]
    return max(ts) if ts else None


def due_step(seq: Sequence, now: datetime, cfg: dict | None = None,
             path: Path | None = None) -> Step | None:
    """The next step, if its scheduled time has arrived AND enough real time has
    elapsed since the last attempt.

    The second condition is not redundant. A sequence opened against a failure
    that is already days old has all of its early steps in the past, so the
    schedule alone would authorise attempt 0, 1 and 2 on three consecutive
    passes minutes apart -- a 14-day ladder collapsed into one afternoon, which
    is the behaviour a customer experiences as harassment. The floor is on
    elapsed real time and so holds regardless of what the schedule says.
    """
    cfg = cfg or load_declines()
    n = current_attempt_no(seq.reference, path)
    if n >= len(seq.steps):
        return None
    step = seq.steps[n]
    if step.due_at > now:
        return None
    floor = float(cfg["compliance"].get("min_hours_between_attempts", 0))
    last = last_attempt_at(seq.reference, path)
    if last is not None and now - last < timedelta(hours=floor):
        return None
    return step


# ---------------------------------------------------------------------------
# STOPPING
# ---------------------------------------------------------------------------

def check_stop(seq: Sequence, now: datetime, observed_state: str | None = None,
               cfg: dict | None = None, path: Path | None = None,
               kill_switch: bool = False) -> str | None:
    """Should this sequence end? Returns a reason, or None to continue.

    Called before EVERY attempt, not only at the end. medusa#16398 is the
    cautionary case: their handler never processed `payment_intent.canceled` or
    `payment_intent.payment_failed`, so orders sat pending forever while
    inventory stayed reserved. A recovery sequence with that hole does not just
    hang -- it keeps escalating at a customer whose payment is already
    definitively dead, which is how a merchant's messaging channel gets blocked.

    So `observed_state` is checked first, and the terminal list is deliberately
    broad: cancelled and expired are terminal exactly like captured is, even
    though no money arrived.
    """
    cfg = cfg or load_declines()
    stop = cfg["stopping"]

    if kill_switch:
        return STOP_KILL_SWITCH
    if ledger.has_succeeded(seq.reference, path):
        return STOP_RECOVERED
    if observed_state and observed_state in stop["terminal_payment_states"]:
        return STOP_TERMINAL_STATE

    n = current_attempt_no(seq.reference, path)
    if n >= len(seq.steps) or n >= int(stop["max_attempts"]):
        return STOP_EXHAUSTED
    if now - seq.opened_at > timedelta(days=float(stop["max_sequence_days"])):
        return STOP_EXPIRED
    if seq.steps[n].channel in cfg["compliance"]["terminal_channels"]:
        return STOP_TERMINAL_CHANNEL
    return None


def stop(seq: Sequence, reason: str, now: datetime, detail: str = "",
         path: Path | None = None) -> None:
    ledger.append(ledger.LedgerEvent(
        at=now, event=ledger.SEQUENCE_STOPPED, reference=seq.reference,
        sequence_id=seq.sequence_id,
        attempt_no=current_attempt_no(seq.reference, path),
        policy_version=seq.policy_version,
        decline_class=seq.classification.decline_class,
        amount_paise=seq.at_risk_paise, detail=detail or reason,
        extra={"stop_reason": reason}), path)


# ---------------------------------------------------------------------------
# COMPLIANCE
# ---------------------------------------------------------------------------

def in_quiet_hours(when: datetime, cfg: dict | None = None) -> bool:
    cfg = cfg or load_declines()
    start, end = cfg["compliance"]["quiet_hours_ist"]
    h = when.astimezone(IST).hour
    return h >= int(start) or h < int(end)   # wraps midnight


def next_permitted(when: datetime, cfg: dict | None = None) -> datetime:
    """The first moment after `when` at which a customer may be contacted.

    Deferral, not cancellation: a step blocked at 23:40 fires at 08:00, it does
    not silently vanish. Dropping it would quietly shorten the sequence and
    make the recovery rate look like a scheduling choice.
    """
    cfg = cfg or load_declines()
    _, end = cfg["compliance"]["quiet_hours_ist"]
    if not in_quiet_hours(when, cfg):
        return when
    local = when.astimezone(IST)
    target = local.replace(hour=int(end), minute=0, second=0, microsecond=0)
    if target <= local:
        target = target + timedelta(days=1)
    return target.astimezone(when.tzinfo or timezone.utc)


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason: str = ""
    defer_until: datetime | None = None
    # Set when the refusal should END the sequence rather than delay it.
    terminal: str | None = None
    # A pause with no date: the ladder waits on something outside itself,
    # such as a promised payment date. Distinct from a deferral, which
    # knows exactly when it resumes, and from a stop, which never does.
    paused: bool = False
    # The step may proceed, but this account cannot actually perform it.
    executable: bool = True


def authorize_contact(seq: Sequence, step: Step, now: datetime,
                      cfg: dict | None = None, path: Path | None = None,
                      sup_path: Path | None = None,
                      notif_path: Path | None = None,
                      wf_cfg: dict | None = None,
                      promise_path: Path | None = None) -> ContactDecision:
    """Compliance gate for anything that reaches a human.

    Silent steps skip this entirely -- there is no one to protect from a
    server-side retry at 3am.
    """
    cfg = cfg or load_declines()
    comp = cfg["compliance"]

    # A PROMISE PAUSES EVERYTHING, including silent steps.
    #
    # Checked above the channel test on purpose: someone who has committed to a
    # date should not be charged again either, not just left unmessaged. A
    # surprise debit on Wednesday from a customer who said Friday is worse than
    # a text.
    if wf_cfg and (wf_cfg.get("promise_to_pay") or {}).get("enabled"):
        held, why = promises.holds(seq.reference, now, wf_cfg, promise_path)
        if held:
            return ContactDecision(False, why, defer_until=None,
                                   terminal=None, paused=True)

    if step.channel not in comp["contacting_channels"]:
        return ContactDecision(True, "silent step; no customer contact")

    # PER-CHANNEL RULES, on top of the shared ones. A phone call is bound by a
    # narrower legal window than a text message, costs twenty times as much and
    # is far more intrusive, so it carries its own floor and its own ordering.
    ch = channels.get(step.channel, wf_cfg)
    ch_ok = channels.authorize(ch, now, seq.at_risk_paise,
                               ledger.contacts_made(
                                   seq.reference, comp["contacting_channels"],
                                   path))
    if not ch_ok.allowed:
        return ContactDecision(False, ch_ok.reason,
                               defer_until=ch_ok.defer_until)
    if not ch_ok.executable:
        return ContactDecision(True, ch_ok.reason, executable=False)

    # SUPPRESSION FIRST. Quiet hours and ceilings answer "when may we contact
    # this person"; an opt-out answers "may we at all". Checking it after the
    # timing rules would mean a suppressed customer is merely deferred.
    if seq.customer_ref is not None:
        if suppression.is_suppressed(seq.customer_ref, sup_path):
            why = suppression.reason_for(seq.customer_ref, sup_path)
            return ContactDecision(False, f"customer opted out ({why})",
                                   terminal=STOP_OPTED_OUT)
        unchecked = ""
    else:
        # No contactable identity on this object -- a Razorpay order carries
        # neither email nor phone. Only the SUPPRESSION check is skipped, and it
        # is reported as skipped rather than silently passing. Everything below
        # still applies: quiet hours and ceilings protect whoever receives the
        # message, and not knowing who that is makes them more necessary, not
        # less. An earlier version returned here and accidentally disabled the
        # whole gate for every abandoned order.
        unchecked = "; opt-out status unchecked (no contactable identity)"

    made = ledger.contacts_made(seq.reference, comp["contacting_channels"], path)
    cap = int(comp["max_contacts_per_reference"])
    if made >= cap:
        return ContactDecision(False, f"contact ceiling reached: {made}/{cap} "
                                      f"for this reference",
                               terminal=STOP_CONTACT_CEILING)

    # PER-CUSTOMER DAILY CAP, finally wired.
    #
    # `max_contacts_per_customer_per_day` sat in declines.yaml since the
    # schedules were written and was read by nothing, because there was no
    # per-customer contact history to count. The per-reference ceiling above
    # counts messages about ONE order -- a customer with six abandoned
    # checkouts could legitimately clear it six times in an afternoon and be
    # messaged eighteen times. This is the ceiling that stops that.
    daily = int(comp.get("max_contacts_per_customer_per_day", 0))
    if daily:
        ok, why = notify.within_daily_cap(seq.customer_ref, now, daily,
                                          notif_path)
        if not ok:
            return ContactDecision(False, why,
                                   defer_until=now + timedelta(days=1))
    if in_quiet_hours(now, cfg):
        nxt = next_permitted(now, cfg)
        return ContactDecision(False, f"quiet hours (IST); deferred to "
                                      f"{nxt.astimezone(IST):%H:%M}",
                               defer_until=nxt)
    # A manual send may occur after link creation. Its own timestamp must also
    # constrain the next automated rung, not just the original attempt time.
    gap = timedelta(hours=float(comp.get("min_hours_between_attempts", 4)))
    message_times = [datetime.fromisoformat(n["at"]) for n in notify.for_reference(seq.reference, notif_path)
                     if n.get("channel") in ("sms", "email") and n.get("status") in (notify.REQUESTED, notify.SENT, notify.DELIVERED, "unknown")]
    if message_times and now < max(message_times) + gap:
        return ContactDecision(False, "minimum gap since last message request",
                               defer_until=max(message_times) + gap)
    return ContactDecision(True, f"contact {made + 1}/{cap}{unchecked}")


# ---------------------------------------------------------------------------
# WHAT AN ALTERNATE RAIL MEANS
# ---------------------------------------------------------------------------

def exclude_methods(seq: Sequence, step: Step) -> list[str]:
    """Which rails to steer the customer away from on this attempt.

    This is where the diagnosis layer we already built pays for itself against
    a metric that matters. None of the prior-art systems know WHY a segment is
    failing -- they retry the same rail on a timer and hope. Ours has an
    attribution ladder, so under `psp_degradation` the alternate-rail step
    steers off the method we have measured to be broken, rather than repeating
    it more politely.

    With no diagnosis, we fall back to excluding the method that actually
    failed, which is weaker but still better than nothing.
    """
    if step.rail != "alternate":
        return []
    if seq.failing_method:
        return [seq.failing_method]
    return []


# ---------------------------------------------------------------------------
# EXECUTION SEAM
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttemptOutcome:
    """What came back. `AMBIGUOUS` is a first-class result, not an error case.

    recoup's rule: on a timeout or an ambiguous response, ask the gateway for
    the truth rather than retrying blindly. A system without an AMBIGUOUS state
    has to decide between two wrong answers -- assume failure and risk a double
    charge, or assume success and lose the money quietly.
    """
    status: str                 # SUCCEEDED | FAILED | DELIVERED | AMBIGUOUS | UNEXECUTABLE
    detail: str = ""
    execution_mode: str = "SIMULATED"
    execution_reference: str | None = None
    execution_url: str | None = None
    amount_paise: int = 0


def write_ahead(seq: Sequence, step: Step, now: datetime,
                path: Path | None = None) -> str:
    """Record the intent, WITH its key, before anything is sent.

    recoup commits its `conversation_id` before the gateway call for the same
    reason. Our key is derived rather than stored, so correctness does not
    actually depend on this record surviving -- but the audit trail does, and an
    operator asking "did we send this twice" needs to see the attempt, not just
    its result.
    """
    key = ledger.idempotency_key(seq.reference, step.attempt_no, seq.policy_version)
    ledger.append(ledger.LedgerEvent(
        at=now, event=ledger.ATTEMPT_INFLIGHT, reference=seq.reference,
        sequence_id=seq.sequence_id, attempt_no=step.attempt_no,
        policy_version=seq.policy_version,
        decline_class=seq.classification.decline_class,
        channel=step.channel, rail=step.rail, idempotency_key=key,
        amount_paise=seq.at_risk_paise,
        detail=f"attempt {step.attempt_no} via {step.channel}"), path)
    return key


def record_outcome(seq: Sequence, step: Step, out: AttemptOutcome,
                   now: datetime, path: Path | None = None) -> None:
    """Commit a DECIDED outcome. This is what advances the sequence.

    Note what is absent: there is no branch here that advances `attempt_no` on
    an ambiguous result. `attempt_no` is derived from the count of decided
    outcomes (`ledger.attempts_made`), so writing an AMBIGUOUS event leaves the
    sequence exactly where it was, and the next pass recomputes the same
    idempotency key. That is medusa#16292 fixed by construction rather than by
    a rule someone has to remember.
    """
    event = {
        "SUCCEEDED": ledger.ATTEMPT_SUCCEEDED,
        "FAILED": ledger.ATTEMPT_FAILED,
        "DELIVERED": ledger.ATTEMPT_DELIVERED,
        "AMBIGUOUS": ledger.ATTEMPT_AMBIGUOUS,
        "UNEXECUTABLE": ledger.ATTEMPT_UNEXECUTABLE,
    }[out.status]
    ledger.append(ledger.LedgerEvent(
        at=now, event=event, reference=seq.reference,
        sequence_id=seq.sequence_id, attempt_no=step.attempt_no,
        policy_version=seq.policy_version,
        decline_class=seq.classification.decline_class,
        channel=step.channel, rail=step.rail,
        idempotency_key=ledger.idempotency_key(
            seq.reference, step.attempt_no, seq.policy_version),
        amount_paise=out.amount_paise if out.status == "SUCCEEDED"
        else seq.at_risk_paise,
        execution_mode=out.execution_mode,
        execution_reference=out.execution_reference,
        execution_url=out.execution_url, detail=out.detail), path)


def record_transport_failure(seq: Sequence, step: Step, detail: str,
                             now: datetime, path: Path | None = None) -> None:
    """The call died before the gateway decided anything.

    Recorded as AMBIGUOUS, which by construction does not advance the sequence.
    The next pass re-issues THIS attempt with THIS key -- a genuine retry that
    the provider can deduplicate, rather than a second charge wearing a new key.
    """
    record_outcome(seq, step, AttemptOutcome("AMBIGUOUS", detail), now, path)


def record_unexecutable(seq: Sequence, step: Step, now: datetime,
                        path: Path | None = None) -> None:
    """A step this account cannot perform: no saved token, no mandate.

    Recorded as UNEXECUTABLE, which -- like AMBIGUOUS -- does not advance the
    sequence, because nothing was attempted. The runner skips past it explicitly
    rather than letting it consume a slot, and it never counts as recovered.
    """
    record_outcome(seq, step, AttemptOutcome(
        "UNEXECUTABLE",
        "requires a saved token or e-mandate; this account has neither, so the "
        "step is reported as unexecutable rather than modelled"), now, path)


def defer(seq: Sequence, step: Step, until: datetime, reason: str,
          now: datetime, path: Path | None = None) -> None:
    ledger.append(ledger.LedgerEvent(
        at=now, event=ledger.CONTACT_DEFERRED, reference=seq.reference,
        sequence_id=seq.sequence_id, attempt_no=step.attempt_no,
        policy_version=seq.policy_version, channel=step.channel,
        amount_paise=seq.at_risk_paise, detail=reason,
        extra={"defer_until": until.isoformat()}), path)


def reconcile(seq: Sequence, observed_state: str | None, now: datetime,
              path: Path | None = None) -> None:
    """Ask the gateway what actually happened; write down the answer.

    Never charges. This is the step that makes UNKNOWN a safe default for an
    unmapped decline reason.
    """
    ledger.append(ledger.LedgerEvent(
        at=now, event=ledger.RECONCILED, reference=seq.reference,
        sequence_id=seq.sequence_id,
        attempt_no=current_attempt_no(seq.reference, path),
        policy_version=seq.policy_version, amount_paise=seq.at_risk_paise,
        detail=f"gateway reports state {observed_state!r}"), path)
