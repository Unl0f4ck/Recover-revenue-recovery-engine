"""Tests for the campaign state derivation the console renders.

The console computes nothing; it renders what this module decides. So the
states it can show, and the invariants it exposes, are pinned here rather than
being whatever the renderer happened to do.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.recovery import dunning as D
from src.recovery import view as V
from src.recovery.declines import classify, classify_abandonment, load_declines

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 8, 28, 10, 0, tzinfo=IST)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "dunning_ledger.jsonl"


@pytest.fixture
def cfg():
    return load_declines()


def _open(cfg, path, reason="insufficient_funds", amount=2_00_000,
          ref="pay_V1", kind="payment_failure", opened=T0):
    cls = (classify_abandonment(cfg) if kind == "checkout_abandoned"
           else classify(reason, cfg))
    seq = D.plan(ref, kind, amount, cls, opened, cfg, failing_method="card")
    seq.extra["failed_at"] = (opened - timedelta(hours=3)).isoformat()
    D.open_sequence(seq, path)
    return seq


def _attempt(seq, step, path, status, at=None, **kw):
    """Run one step the way `campaign.advance` does: write ahead, then record.

    Tests that skip the write-ahead do not exercise the pacing floor, and so
    quietly disagree with production about what state a campaign is in.
    """
    at = at or step.due_at
    if not step.requires_mandate:
        D.write_ahead(seq, step, at, path)
    D.record_outcome(seq, step, D.AttemptOutcome(status, **kw), at, path)


def _skip_silent(seq, path):
    """Advance past a leading silent step, as this account always must."""
    for s in seq.steps:
        if not s.requires_mandate:
            return s
        D.record_unexecutable(seq, s, s.due_at, path)
    raise AssertionError("schedule has no executable step")


def _only(cfg, path, now):
    cs = V.campaigns(now, cfg, path)
    assert len(cs) == 1
    return cs[0]


# ---------------------------------------------------------------------------
# states
# ---------------------------------------------------------------------------

def test_a_fresh_sequence_is_waiting(cfg, path):
    _open(cfg, path)
    assert _only(cfg, path, T0 + timedelta(minutes=5)).state == V.WAITING


def test_a_due_step_shows_as_due(cfg, path):
    _open(cfg, path)
    assert _only(cfg, path, T0 + timedelta(hours=49)).state == V.DUE


def test_delivered_is_not_recovered(cfg, path):
    """The distinction the whole console is built around.

    A link that was created and sent is a contact that happened. Whether the
    customer paid is unknown until the API is read. Rendering the first as the
    second is how a send rate gets reported as a recovery rate.
    """
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)            # first contacting step
    _attempt(seq, step, path, "DELIVERED", detail="link created",
             execution_mode="REAL", execution_reference="plink_1")

    c = _only(cfg, path, step.due_at + timedelta(minutes=10))
    assert c.state == V.DELIVERED
    assert c.state != V.RECOVERED
    assert c.recovered_paise == 0
    assert not c.invariants.already_succeeded
    assert c.invariants.last_reconciliation == "link status not yet read back"


def test_delivered_becomes_recovered_only_via_reconciliation(cfg, path):
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)
    _attempt(seq, step, path, "DELIVERED", detail="link created")
    assert _only(cfg, path, step.due_at).state == V.DELIVERED

    D.record_outcome(seq, step, D.AttemptOutcome(
        "SUCCEEDED", "link paid", amount_paise=2_00_000),
        step.due_at + timedelta(days=1), path)
    c = _only(cfg, path, step.due_at + timedelta(days=1))
    assert c.state == V.RECOVERED
    assert c.recovered_paise == 2_00_000


def test_quiet_hold_is_its_own_state(cfg, path):
    seq = _open(cfg, path)
    night = datetime(2026, 8, 30, 23, 30, tzinfo=IST)
    step = _skip_silent(seq, path)
    D.defer(seq, step, D.next_permitted(night, cfg), "quiet hours", night, path)
    c = _only(cfg, path, night)
    assert c.state == V.QUIET_HOLD
    assert c.invariants.quiet_hold_until is not None


def test_quiet_hold_clears_once_the_attempt_runs(cfg, path):
    seq = _open(cfg, path)
    night = datetime(2026, 8, 30, 23, 30, tzinfo=IST)
    step = _skip_silent(seq, path)
    D.defer(seq, step, D.next_permitted(night, cfg), "quiet hours", night, path)
    morning = datetime(2026, 8, 31, 8, 5, tzinfo=IST)
    _attempt(seq, step, path, "DELIVERED", at=morning, detail="sent")
    assert _only(cfg, path, morning).state == V.DELIVERED


def test_retry_scheduled_after_a_failed_attempt(cfg, path):
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)
    _attempt(seq, step, path, "FAILED", detail="declined")
    assert _only(cfg, path, step.due_at + timedelta(minutes=1)).state == \
        V.RETRY_SCHEDULED


def test_escalation_and_write_off_are_distinguished(cfg, path):
    """Both are `terminal_channel_reached`; they are not the same outcome."""
    seq = _open(cfg, path)                       # 'slow' ends in human_review
    for s in seq.steps[:-1]:
        D.record_outcome(seq, s, D.AttemptOutcome("FAILED", "x"), s.due_at, path)
    D.stop(seq, D.STOP_TERMINAL_CHANNEL, seq.steps[-1].due_at, path=path)
    assert _only(cfg, path, seq.steps[-1].due_at).state == V.ESCALATED

    p2 = path.parent / "b.jsonl"
    seq2 = _open(cfg, p2, kind="checkout_abandoned", ref="order_V2")  # ends write_off
    for s in seq2.steps[:-1]:
        D.record_outcome(seq2, s, D.AttemptOutcome("FAILED", "x"), s.due_at, p2)
    D.stop(seq2, D.STOP_TERMINAL_CHANNEL, seq2.steps[-1].due_at, path=p2)
    assert _only(cfg, p2, seq2.steps[-1].due_at).state == V.WRITTEN_OFF


def test_terminal_payment_state_closes_the_campaign(cfg, path):
    seq = _open(cfg, path)
    D.stop(seq, D.STOP_TERMINAL_STATE, T0 + timedelta(hours=1), path=path)
    assert _only(cfg, path, T0 + timedelta(hours=2)).state == V.CLOSED_TERMINAL


def test_every_state_is_either_live_or_terminal(cfg, path):
    _open(cfg, path)
    c = _only(cfg, path, T0)
    assert c.state in V._ORDER
    assert c.terminal == (c.state in V.TERMINAL)


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------

def test_timeline_shows_the_whole_ladder_not_just_the_latest_action(cfg, path):
    """Showing only the most recent step is what made the old console undersell
    the system. A plan is legible only if the unrun part is visible too."""
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)
    t = step.due_at
    _attempt(seq, step, path, "DELIVERED", detail="sent")
    tl = _only(cfg, path, t).timeline
    assert any(e.kind == "future" for e in tl), "no scheduled steps rendered"
    assert any(e.kind == "past" for e in tl)
    # every past entry precedes every scheduled one
    kinds = [e.kind for e in tl]
    assert kinds.index("future") == len([k for k in kinds if k == "past"])


def test_timeline_follows_ledger_order_not_timestamp_order(cfg, path):
    """A skewed clock must not reorder the story.

    The ledger is append-only, so the order events were written IS the order
    they happened. The `at` field is only what the writing process believed the
    time was -- and a process handed a shifted clock (a preview flag, a bad
    container clock, an NTP jump) can stamp an attempt in the future. Sorting
    the timeline by `at` renders that as fact: the recovery appears BEFORE the
    link that produced it, which is an audit trail arguing against causality.
    """
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)
    skewed = step.due_at + timedelta(hours=6)          # clock ran ahead
    _attempt(seq, step, path, "DELIVERED", at=skewed, detail="link sent")
    truth = step.due_at + timedelta(hours=1)           # actually later, stamped earlier
    D.record_outcome(seq, step, D.AttemptOutcome(
        "SUCCEEDED", "link paid", amount_paise=2_00_000), truth, path)

    tl = [e for e in _only(cfg, path, skewed).timeline if e.kind == "past"]
    labels = [e.label.lower() for e in tl]
    sent = next(i for i, l in enumerate(labels) if "sent" in l)
    won = next(i for i, l in enumerate(labels) if "paid" in l)
    assert sent < won, ("the recovery rendered before the link that produced "
                        "it; the timeline sorted by timestamp, not by ledger "
                        "order")


def test_timeline_opens_with_the_original_failure(cfg, path):
    _open(cfg, path)
    tl = _only(cfg, path, T0).timeline
    # Plain language, so assert the MEANING rather than a phrase: the first
    # entry is the thing that went wrong, before anything the system did.
    assert any(w in tl[0].label.lower() for w in ("failed", "at risk")), tl[0].label
    assert tl[0].at < tl[1].at, "failure must precede classification"


def test_a_stopped_campaign_shows_no_future_steps(cfg, path):
    seq = _open(cfg, path)
    D.stop(seq, D.STOP_TERMINAL_STATE, T0 + timedelta(hours=1), path=path)
    tl = _only(cfg, path, T0 + timedelta(hours=2)).timeline
    assert not any(e.kind == "future" for e in tl)


def test_unexecutable_silent_retry_is_shown_not_hidden(cfg, path):
    seq = _open(cfg, path)
    D.record_unexecutable(seq, seq.steps[0], seq.steps[0].due_at, path)
    tl = _only(cfg, path, seq.steps[0].due_at).timeline
    assert any("could not retry" in e.label.lower() for e in tl),         [e.label for e in tl]


# ---------------------------------------------------------------------------
# invariants surfaced to the reviewer
# ---------------------------------------------------------------------------

def test_invariants_expose_every_safety_property(cfg, path):
    _open(cfg, path)
    i = _only(cfg, path, T0).invariants
    assert i.attempt.endswith(f"of {len(load_declines()['schedules']['slow']['steps'])}")
    assert i.retryable is True
    assert i.decline_class == "SOFT_FUNDS"
    assert i.next_action and i.next_due
    assert i.idempotency_key and i.idempotency_key.startswith("rcv_")
    assert i.contacts_used.endswith(
        f"of {cfg['compliance']['max_contacts_per_reference']}")
    assert i.stop_reason is None


def test_hard_declines_are_shown_as_not_retryable(cfg, path):
    _open(cfg, path, reason="card_expired")
    i = _only(cfg, path, T0).invariants
    assert i.retryable is False
    assert i.decline_class == "HARD_INSTRUMENT"


def test_stop_reason_is_surfaced_once_stopped(cfg, path):
    seq = _open(cfg, path)
    D.stop(seq, D.STOP_EXHAUSTED, T0 + timedelta(days=1), path=path)
    i = _only(cfg, path, T0 + timedelta(days=2)).invariants
    assert i.stop_reason == D.STOP_EXHAUSTED
    assert i.next_action is None


def test_execution_mode_is_carried_through(cfg, path):
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)
    _attempt(seq, step, path, "DELIVERED", detail="x", execution_mode="REAL")
    assert _only(cfg, path, step.due_at).invariants.execution_mode == "REAL"


# ---------------------------------------------------------------------------
# funnel
# ---------------------------------------------------------------------------

def test_funnel_reports_the_zero_that_explains_the_ceiling(cfg, path):
    """`silent retries executed: 0` is the finding, not an empty row to hide."""
    _open(cfg, path)
    rows = dict((k, v) for k, v, _ in V.funnel(V.campaigns(T0, cfg, path), path))
    assert rows["Silent retries executed"] == 0
    assert "Silent retries executed" in rows


def test_funnel_separates_delivered_from_recovered(cfg, path):
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)
    _attempt(seq, step, path, "DELIVERED", detail="x")
    rows = dict((k, v) for k, v, _ in
                V.funnel(V.campaigns(step.due_at, cfg, path), path))
    assert rows["Links delivered"] == 1
    assert rows["Recovered"] == 0


def test_one_attempt_reconciled_to_success_counts_once(cfg, path):
    """A single attempt that is later reconciled is still ONE attempt.

    A link is DELIVERED, and days later the same attempt is read back from the
    API as SUCCEEDED. Both are decided outcomes of attempt 0. Counting decided
    EVENTS rather than distinct attempts made a recovered campaign report
    "attempt 3 of 4" after one contact, and lit a rung of the ladder that had
    never run -- the console claiming work it had not done.
    """
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)                 # attempt 0 is the silent one
    _attempt(seq, step, path, "DELIVERED", detail="link sent")
    before = D.current_attempt_no(seq.reference, path)

    D.record_outcome(seq, step, D.AttemptOutcome(
        "SUCCEEDED", "link paid", amount_paise=2_00_000),
        step.due_at + timedelta(days=1), path)

    assert D.current_attempt_no(seq.reference, path) == before, (
        "reconciling an attempt to success advanced the attempt counter")

    c = _only(cfg, path, step.due_at + timedelta(days=1))
    assert c.state == V.RECOVERED
    done = [s for s in c.steps if s["done"]]
    assert len(done) == before, "ladder marked a rung done that never ran"


def test_a_reconciled_success_reports_as_reconciled(cfg, path):
    """`reconcile_links` records the API read as ATTEMPT_SUCCEEDED, which IS the
    reconciliation. Looking only for RECONCILED events made a campaign that had
    just been reconciled say "link status not yet read back"."""
    seq = _open(cfg, path)
    step = _skip_silent(seq, path)
    _attempt(seq, step, path, "DELIVERED", detail="link sent")
    assert _only(cfg, path, step.due_at).invariants.last_reconciliation == \
        "link status not yet read back"

    t = step.due_at + timedelta(days=1)
    D.record_outcome(seq, step, D.AttemptOutcome(
        "SUCCEEDED", "payment link plink_X paid", amount_paise=2_00_000), t, path)
    got = _only(cfg, path, t).invariants.last_reconciliation
    assert got and "not yet" not in got, got
