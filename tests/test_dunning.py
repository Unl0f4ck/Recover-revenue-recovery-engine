"""Tests for the retry sequencer.

The first two are the ones that matter, and they are named after the bug
reports they encode. Everything else in this file is ordinary coverage; those
two are the reason the module is shaped the way it is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.recovery import dunning as D
from src.recovery import ledger as L
from src.recovery.declines import classify, classify_abandonment, load_declines

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 8, 28, 10, 0, tzinfo=IST)      # 10:00 IST, well outside quiet hours


@pytest.fixture
def path(tmp_path):
    return tmp_path / "dunning_ledger.jsonl"


@pytest.fixture
def cfg():
    return load_declines()


def _seq(cfg, reason="bank_technical_error", amount=2_00_000, ref="pay_TEST1",
         opened=T0, **kw):
    return D.plan(ref, "payment_failure", amount, classify(reason, cfg),
                  opened, cfg, **kw)


# ---------------------------------------------------------------------------
# medusajs/medusa#16292
# ---------------------------------------------------------------------------

def test_medusa_16292_transport_failure_reuses_idempotency_key(cfg, path):
    """A transport failure must re-issue the SAME attempt with the SAME key.

    Medusa's bug: `capturePayment` forwards a Capture row's ID as the provider
    idempotency key, then deletes that row when the call fails. The retry mints
    a new row, a new ID, and therefore a key the provider has never seen -- so
    the provider cannot deduplicate and the customer is charged twice.

    The defect is not "they forgot to retry". It is that FAILING advanced the
    identity of the operation. This test pins the opposite: an undecided
    outcome leaves the sequence exactly where it was.
    """
    seq = _seq(cfg, "bank_technical_error")
    D.open_sequence(seq, path)

    step = seq.steps[D.current_attempt_no(seq.reference, path)]
    key_first = D.write_ahead(seq, step, T0, path)

    # the call dies mid-flight -- no decision from the gateway
    D.record_transport_failure(seq, step, "socket timeout", T0, path)

    # the sequence has NOT advanced
    assert D.current_attempt_no(seq.reference, path) == 0, (
        "an ambiguous outcome advanced the attempt counter, which is exactly "
        "what mints a fresh idempotency key (#16292)")

    step_again = seq.steps[D.current_attempt_no(seq.reference, path)]
    key_second = D.write_ahead(seq, step_again, T0 + timedelta(minutes=5), path)

    assert key_second == key_first, (
        "retry after a transport failure used a different idempotency key; "
        "the provider cannot deduplicate and this double-charges")
    assert step_again.attempt_no == step.attempt_no


def test_key_survives_total_process_loss(cfg):
    """The key is derived, not stored, so nothing can lose it.

    Medusa lost the key by deleting the row that held it. A key recomputed from
    durable facts cannot be lost that way -- there is no row to delete.
    """
    a = L.idempotency_key("pay_X", 2, "dunning-1.0")
    b = L.idempotency_key("pay_X", 2, "dunning-1.0")
    assert a == b
    assert a != L.idempotency_key("pay_X", 3, "dunning-1.0")   # next attempt
    assert a != L.idempotency_key("pay_Y", 2, "dunning-1.0")   # other reference
    assert a != L.idempotency_key("pay_X", 2, "dunning-1.1")   # policy changed


def test_a_decided_failure_does_advance(cfg, path):
    """The counterpart. A real decline is a completed attempt and moves on."""
    seq = _seq(cfg)
    D.open_sequence(seq, path)
    step = seq.steps[0]
    D.write_ahead(seq, step, T0, path)
    D.record_outcome(seq, step, D.AttemptOutcome("FAILED", "declined"), T0, path)
    assert D.current_attempt_no(seq.reference, path) == 1


def test_unexecutable_step_is_skipped_not_retried_forever(cfg, path):
    """A step this account cannot perform must ADVANCE the ladder.

    This asserted the opposite for a while, on the reasoning that a step we did
    not perform is not a step we performed. True, and it deadlocks: a silent
    retry needs a mandate we do not have, so the sequence would re-select it on
    every pass and never reach the contacting rungs beneath it. Both schedules
    that matter for failed payments -- `fast` and `slow` -- open on a silent
    step, so the bug would have disabled the entire failed-payment path while
    the abandonment path, which has no silent step, looked fine.
    """
    seq = _seq(cfg, "insufficient_funds")           # 'slow' opens on silent
    D.open_sequence(seq, path)
    assert seq.steps[0].requires_mandate
    D.record_unexecutable(seq, seq.steps[0], T0, path)
    assert D.current_attempt_no(seq.reference, path) == 1, (
        "an unexecutable step did not advance; the sequence deadlocks on it")
    assert not seq.steps[1].requires_mandate, "next rung should be reachable"


# ---------------------------------------------------------------------------
# medusajs/medusa#16398
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["captured", "paid", "refunded",
                                   "cancelled", "expired"])
def test_medusa_16398_terminal_state_stops_the_sequence(cfg, path, state):
    """Every terminal state must end the sequence, including the unwelcome ones.

    Medusa's bug: the webhook subscriber returned early on
    `payment_intent.canceled` and `payment_intent.payment_failed` -- "We
    currently don't handle these payment statuses" -- so Stripe said canceled
    and Medusa said pending, forever, with inventory still reserved.

    For a dunning system that hole is worse than a stuck order: a sequence that
    cannot observe termination keeps escalating at a customer whose payment is
    already dead. `cancelled` and `expired` are parametrised here deliberately;
    those are the two everybody forgets, because no money arrived and they do
    not look like endings.
    """
    seq = _seq(cfg)
    D.open_sequence(seq, path)
    reason = D.check_stop(seq, T0 + timedelta(hours=2), observed_state=state,
                          cfg=cfg, path=path)
    assert reason == D.STOP_TERMINAL_STATE, (
        f"observed terminal state {state!r} did not stop the sequence (#16398)")


def test_no_path_leaves_a_sequence_live(cfg, path):
    """Exhausting the schedule terminates rather than hanging."""
    seq = _seq(cfg)
    D.open_sequence(seq, path)
    for s in seq.steps:
        if D.check_stop(seq, s.due_at, cfg=cfg, path=path):
            break
        D.record_outcome(seq, s, D.AttemptOutcome("FAILED", "declined"),
                         s.due_at, path)
    reason = D.check_stop(seq, T0 + timedelta(days=30), cfg=cfg, path=path)
    assert reason is not None, "sequence ran out of steps but never stopped"


def test_sequence_expires_on_age(cfg, path):
    seq = _seq(cfg)
    D.open_sequence(seq, path)
    late = T0 + timedelta(days=int(cfg["stopping"]["max_sequence_days"]) + 1)
    assert D.check_stop(seq, late, cfg=cfg, path=path) in (
        D.STOP_EXPIRED, D.STOP_EXHAUSTED)


# ---------------------------------------------------------------------------
# recoup: at most one success per reference
# ---------------------------------------------------------------------------

def test_one_success_per_reference(cfg, path):
    seq = _seq(cfg)
    D.open_sequence(seq, path)
    D.record_outcome(seq, seq.steps[0],
                     D.AttemptOutcome("SUCCEEDED", "paid", amount_paise=2_00_000),
                     T0, path)
    assert L.has_succeeded(seq.reference, path)
    assert D.check_stop(seq, T0 + timedelta(hours=1), cfg=cfg,
                        path=path) == D.STOP_RECOVERED


def test_success_is_read_from_the_ledger_not_memory(cfg, path):
    """A recovery link outlives the process that created it.

    Building a fresh Sequence object -- as a later batch run would -- must not
    resurrect a sequence that already succeeded.
    """
    seq = _seq(cfg)
    D.open_sequence(seq, path)
    D.record_outcome(seq, seq.steps[0],
                     D.AttemptOutcome("SUCCEEDED", "paid", amount_paise=2_00_000),
                     T0, path)
    fresh = _seq(cfg)                      # new object, same reference
    assert D.check_stop(fresh, T0 + timedelta(days=1), cfg=cfg,
                        path=path) == D.STOP_RECOVERED


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["card_expired", "debit_instrument_blocked",
                                    "card_number_invalid",
                                    "international_transaction_not_allowed"])
def test_hard_declines_are_never_retried(cfg, reason):
    """Universal across every system in docs/PRIOR_ART.md."""
    c = classify(reason, cfg)
    assert c.decline_class == "HARD_INSTRUMENT"
    assert not c.retryable


@pytest.mark.parametrize("reason", ["insufficient_funds", "bank_technical_error",
                                    "gateway_technical_error",
                                    "transaction_limit_exceeded"])
def test_soft_declines_are_retryable(cfg, reason):
    assert classify(reason, cfg).retryable


def test_unmapped_reason_reconciles_rather_than_retrying(cfg):
    """The safety property: a reason Razorpay adds tomorrow cannot cause a charge."""
    c = classify("some_code_invented_next_year", cfg)
    assert c.decline_class == "UNKNOWN"
    assert not c.retryable
    assert c.needs_reconciliation
    assert not c.mapped


def test_auth_failures_are_not_silently_retried(cfg):
    """OTP failures need the customer present; a server-side re-present fails
    at the identical step."""
    c = classify("otp_attempts_exceeded", cfg)
    assert not c.retryable
    assert c.schedule == "needs_customer"
    steps = D.plan("p", "payment_failure", 5_00_000, c, T0, cfg).steps
    assert all(s.channel != "silent" for s in steps)


def test_abandonment_is_not_an_unknown_decline(cfg):
    c = classify_abandonment(cfg)
    assert c.decline_class == "ABANDONED"
    assert not c.needs_reconciliation


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------

def test_schedules_are_spaced_in_hours_not_seconds(cfg):
    """The load-bearing property of a dunning schedule.

    Retrying a declined payment a minute later fails for the same reason it
    failed the first time; the whole mechanism is that enough time passes for
    the cause to change.
    """
    for name, spec in cfg["schedules"].items():
        gaps = [s["after_hours"] for s in spec["steps"]]
        assert gaps == sorted(gaps), f"{name} schedule is not monotonic"
        assert max(gaps) >= 24, f"{name} never waits a day"


def test_funds_schedule_is_slower_than_technical(cfg):
    """Two speeds, the pattern every prior-art system converged on."""
    slow = classify("insufficient_funds", cfg).schedule
    fast = classify("gateway_technical_error", cfg).schedule
    assert cfg["schedules"][slow]["steps"][0]["after_hours"] > \
           cfg["schedules"][fast]["steps"][0]["after_hours"]


def test_steps_are_offset_from_the_original_failure(cfg):
    """Not from the previous attempt, so deferrals cannot make the schedule drift."""
    seq = _seq(cfg, "insufficient_funds")
    hours = [(s.due_at - seq.opened_at).total_seconds() / 3600 for s in seq.steps]
    assert hours == sorted(hours)
    assert hours[0] == pytest.approx(48)


def test_step_count_respects_the_cap(cfg):
    seq = _seq(cfg, "insufficient_funds")
    assert len(seq.steps) <= int(cfg["stopping"]["max_attempts"])


def test_due_step_waits_for_its_time(cfg, path):
    seq = _seq(cfg, "insufficient_funds")
    D.open_sequence(seq, path)
    assert D.due_step(seq, T0 + timedelta(hours=1), cfg, path) is None
    assert D.due_step(seq, T0 + timedelta(hours=49), cfg, path) is not None


def test_backdated_sequence_cannot_collapse_into_one_afternoon(cfg, path):
    """A schedule whose early steps are all already due must still be paced.

    This is the failure mode a backfill creates: open a sequence against a
    week-old failure and every step up to day 7 is "due", so consecutive passes
    would fire attempt 0, 1 and 2 minutes apart. The customer experiences a
    14-day ladder as three messages in one afternoon. The floor is on elapsed
    real time, so it holds no matter what the schedule says.
    """
    seq = _seq(cfg, "insufficient_funds")
    D.open_sequence(seq, path)
    far_future = T0 + timedelta(days=10)          # every early step is due

    step = D.due_step(seq, far_future, cfg, path)
    assert step is not None
    D.write_ahead(seq, step, far_future, path)
    D.record_outcome(seq, step, D.AttemptOutcome("FAILED", "declined"),
                     far_future, path)

    floor = float(cfg["compliance"]["min_hours_between_attempts"])
    assert D.due_step(seq, far_future + timedelta(minutes=1), cfg, path) is None, (
        "a second attempt fired immediately after the first")
    assert D.due_step(seq, far_future + timedelta(hours=floor + 0.1),
                      cfg, path) is not None


# ---------------------------------------------------------------------------
# compliance
# ---------------------------------------------------------------------------

def test_no_customer_contact_in_quiet_hours(cfg, path):
    seq = _seq(cfg, "insufficient_funds")
    D.open_sequence(seq, path)
    night = datetime(2026, 8, 29, 23, 30, tzinfo=IST)
    contact = next(s for s in seq.steps if s.channel == "payment_link")
    d = D.authorize_contact(seq, contact, night, cfg, path)
    assert not d.allowed
    assert d.defer_until is not None
    assert d.defer_until.astimezone(IST).hour == 8


def test_quiet_hours_defer_rather_than_drop(cfg):
    """A deferred step still fires. Dropping it would quietly shorten the
    sequence and make the recovery rate a scheduling artefact."""
    night = datetime(2026, 8, 29, 23, 30, tzinfo=IST)
    nxt = D.next_permitted(night, cfg)
    assert nxt > night
    assert not D.in_quiet_hours(nxt, cfg)


def test_silent_steps_ignore_quiet_hours(cfg, path):
    """Nobody needs protecting from a server-side retry at 3am."""
    seq = _seq(cfg, "insufficient_funds")
    D.open_sequence(seq, path)
    night = datetime(2026, 8, 29, 3, 0, tzinfo=IST)
    silent = next(s for s in seq.steps if s.channel == "silent")
    assert D.authorize_contact(seq, silent, night, cfg, path).allowed


def test_contact_ceiling_is_enforced(cfg, path):
    seq = _seq(cfg, "insufficient_funds")
    D.open_sequence(seq, path)
    contact = next(s for s in seq.steps if s.channel == "payment_link")
    cap = int(cfg["compliance"]["max_contacts_per_reference"])
    for i in range(cap):
        D.write_ahead(seq, D.Step(i, T0, "payment_link", "same"), T0, path)
    d = D.authorize_contact(seq, contact, T0, cfg, path)
    assert not d.allowed
    assert "ceiling" in d.reason


# ---------------------------------------------------------------------------
# stopping rules and rails
# ---------------------------------------------------------------------------

def test_exposure_floor_prevents_opening(cfg):
    floor = int(cfg["stopping"]["min_at_risk_paise"])
    assert not D.may_open(floor - 1, cfg)[0]
    assert D.may_open(floor, cfg)[0]


def test_kill_switch_stops_everything(cfg, path):
    seq = _seq(cfg)
    D.open_sequence(seq, path)
    assert D.check_stop(seq, T0, cfg=cfg, path=path,
                        kill_switch=True) == D.STOP_KILL_SWITCH


def test_alternate_rail_steers_off_the_failing_method(cfg):
    seq = _seq(cfg, "gateway_technical_error", failing_method="upi",
               diagnosis_family="psp_degradation")
    alt = next(s for s in seq.steps if s.rail == "alternate")
    # A diagnosed outage may now put every planned retry on an alternate rail.
    # Exercise the same-rail exclusion rule independently of that policy choice.
    same = D.Step(alt.attempt_no, alt.due_at, alt.channel, "same")
    assert D.exclude_methods(seq, alt) == ["upi"]
    assert D.exclude_methods(seq, same) == []


def test_ledger_is_append_only(cfg, path):
    """No update, no delete surface -- a correction is itself an event."""
    seq = _seq(cfg)
    D.open_sequence(seq, path)
    before = len(L.read(path))
    D.record_outcome(seq, seq.steps[0], D.AttemptOutcome("FAILED", "x"), T0, path)
    after = L.read(path)
    assert len(after) == before + 1
    assert not hasattr(L, "update") and not hasattr(L, "delete")


# ---------------------------------------------------------------------------
# the sequencer must not eat its own tail
# ---------------------------------------------------------------------------

def test_our_own_recovery_links_are_not_revenue_at_risk():
    """A recovery link is an unpaid payment link, which is what an abandoned
    checkout looks like from the ingestion seam.

    Left alone the sequencer opens a campaign against its own output -- link
    begets campaign begets link, compounding on every pass, all of it counted
    as fresh revenue at risk. The 30-minute grace period hides it: a link only
    becomes stale well after the run that created it has finished, so a single
    pass looks clean and the loop appears on the next one.
    """
    from datetime import datetime as _dt
    from src.execution.razorpay import RECOVERY_TAG
    from src.ingest.razorpay_source import IST as _IST, abandoned_checkouts

    now = _dt(2026, 8, 28, 12, 0, tzinfo=_IST)
    old = int((now - timedelta(hours=3)).timestamp())
    ours = {"id": "plink_OURS", "status": "created", "created_at": old,
            "amount": 5_00_000, "amount_paid": 0, "order_id": "order_OURS",
            "notes": {"source": RECOVERY_TAG}}
    theirs = {"id": "plink_CUSTOMER", "status": "created", "created_at": old,
              "amount": 5_00_000, "amount_paid": 0, "order_id": "order_THEIRS",
              "notes": {"source": "merchant-checkout"}}

    got = abandoned_checkouts([], [ours, theirs], [], now=now)
    refs = {r.reference for r in got}
    assert "plink_OURS" not in refs, "the sequencer would dun its own link"
    assert "plink_CUSTOMER" in refs, "a genuine unpaid link must still be chased"
