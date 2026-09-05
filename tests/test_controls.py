"""Tests for the operational controls, and for the ones that were dead.

Three of these were declared in config, plumbed through the code as parameters,
and read by nothing on the sequencer path. A safety control that is documented
and dead is worse than one never written, because everyone downstream believes
it works. These tests exist so that cannot recur silently.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.recovery import dunning as D
from src.recovery import ledger as L
from src.recovery import notify, runlock, suppression
from src.recovery.campaign import run_pass
from src.recovery.declines import classify, load_declines
from src.ingest.razorpay_source import RevenueAtRisk

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 8, 28, 12, 0, tzinfo=IST)          # midday, outside quiet hours


@pytest.fixture
def cfg():
    return load_declines()


@pytest.fixture
def paths(tmp_path):
    return {"path": tmp_path / "ledger.jsonl",
            "sup_path": tmp_path / "suppression.jsonl",
            "notif_path": tmp_path / "notifications.jsonl",
            "lock": tmp_path / "run.lock"}


def _items(n=3, amount=8_00_000, customer=None,
           reason="otp_attempts_exceeded"):
    """Auth failures by default: the `needs_customer` schedule opens on a
    CONTACT, with no silent rung in front of it.

    That matters for these tests. The `slow` schedule opens on a silent retry,
    which on this account is unexecutable -- it advances the ladder but spends
    no exposure and contacts nobody. A test written against it appears to
    exercise the contact path and does not.
    """
    # DISTINCT customers by default. Sharing one collides with the
    # per-customer daily cap, and a test for the per-RUN ceiling that is
    # really being stopped by the per-CUSTOMER ceiling tests the wrong
    # control while looking like it passes.
    return [RevenueAtRisk(
        kind="payment_failure", reference=f"pay_{i}",
        created_at=T0 - timedelta(hours=2), amount_paise=amount,
        segment="HDFC", method="card",
        detail={"error_reason": reason,
                "customer_ref": customer or f"c{i}@example.com"})
        for i in range(n)]


def _policy(**over):
    base = {"bounds": {"global_kill_switch": False},
            "batch_limits": {"max_interventions_per_run": 50,
                             "max_exposure_per_run_paise": 100_000_000}}
    for k, v in over.items():
        if k in ("global_kill_switch",):
            base["bounds"][k] = v
        else:
            base["batch_limits"][k] = v
    return base


# ---------------------------------------------------------------------------
# the kill switch
# ---------------------------------------------------------------------------

def test_kill_switch_is_read_from_config_not_just_accepted(cfg, paths):
    """It was threaded through every function and read by nothing.

    `bounds.global_kill_switch` sat in policy.yaml, was accepted as a parameter
    by `run_pass`, `advance` and `check_stop`, and defaulted to False on the
    production path forever because no caller ever loaded it.
    """
    res = run_pass(_items(), T0, cfg, dry_run=True, path=paths["path"],
                   sup_path=paths["sup_path"],
                   notif_path=paths["notif_path"], policy=_policy(global_kill_switch=True))
    assert res.halted, "kill switch set in config did not halt the run"
    assert not res.opened, "a halted run still opened campaigns"
    assert not res.advanced, "a halted run still executed attempts"
    assert L.read(paths["path"]) == [], "a halted run still wrote to the ledger"


def test_kill_switch_off_lets_the_run_proceed(cfg, paths):
    res = run_pass(_items(), T0, cfg, dry_run=True, path=paths["path"],
                   sup_path=paths["sup_path"],
             notif_path=paths["notif_path"], policy=_policy())
    assert not res.halted
    assert len(res.opened) == 3


# ---------------------------------------------------------------------------
# per-run ceilings
# ---------------------------------------------------------------------------

def test_per_run_intervention_cap_applies_to_the_sequencer(cfg, paths):
    """Read only by the OLD one-shot runner; the sequencer ran uncapped."""
    items = _items(5)
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"],
             notif_path=paths["notif_path"], policy=_policy())          # open them
    res = run_pass(items, T0 + timedelta(hours=2), cfg, dry_run=True,
                   path=paths["path"], sup_path=paths["sup_path"],
                   notif_path=paths["notif_path"], policy=_policy(max_interventions_per_run=2))
    assert len(res.advanced) == 2
    assert "intervention cap" in res.halted


def test_per_run_exposure_cap_applies_to_the_sequencer(cfg, paths):
    items = _items(5, amount=6_00_000)
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"],
             notif_path=paths["notif_path"], policy=_policy())
    res = run_pass(items, T0 + timedelta(hours=2), cfg, dry_run=True,
                   path=paths["path"], sup_path=paths["sup_path"],
                   notif_path=paths["notif_path"], policy=_policy(max_exposure_per_run_paise=12_00_000))
    # Stops on the attempt that CROSSES the cap, so the cap may be exceeded by
    # at most one item -- checking before spending would need to know the cost
    # in advance, which for a contact is only known once it has been made.
    assert 12_00_000 <= res.spent_paise <= 18_00_000
    assert "exposure cap" in res.halted


# ---------------------------------------------------------------------------
# opt-out
# ---------------------------------------------------------------------------

def test_a_suppressed_customer_is_never_contacted(cfg, paths):
    """The one control the customer holds. Absolute, and above the timing rules."""
    suppression.suppress("a@example.com", T0, "replied STOP",
                         path=paths["sup_path"])
    items = _items(2, customer="a@example.com")
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"],
             notif_path=paths["notif_path"], policy=_policy())
    res = run_pass(items, T0 + timedelta(hours=2), cfg, dry_run=True,
                   path=paths["path"], sup_path=paths["sup_path"],
                   notif_path=paths["notif_path"], policy=_policy())
    assert not res.advanced, "a suppressed customer was contacted"
    assert any(s["reason"] == D.STOP_OPTED_OUT for s in res.stopped), \
        "an opt-out was recorded as something else"
    assert any("opted out" in s.get("detail", "") for s in res.stopped)


def test_suppression_beats_a_perfectly_timed_high_value_contact(cfg, paths):
    """Suppression is checked ABOVE quiet hours and ceilings: those answer WHEN
    we may contact someone, this answers WHETHER."""
    seq = D.plan("pay_X", "payment_failure", 90_00_000,
                 classify("insufficient_funds", cfg), T0, cfg,
                 customer_ref="vip@example.com")
    step = next(s for s in seq.steps if not s.requires_mandate)
    ok = D.authorize_contact(seq, step, T0, cfg, paths["path"],
                             paths["sup_path"], paths["notif_path"])
    assert ok.allowed                                   # midday, no ceiling hit

    suppression.suppress("VIP@Example.com ", T0, "spam complaint",
                         action=suppression.COMPLAINT, path=paths["sup_path"])
    blocked = D.authorize_contact(seq, step, T0, cfg, paths["path"],
                                  paths["sup_path"], paths["notif_path"])
    assert not blocked.allowed
    assert blocked.defer_until is None, "an opt-out was treated as a deferral"


def test_suppression_normalises_identity(paths):
    """Whitespace and case must not defeat an opt-out."""
    suppression.suppress("Foo@Example.COM", T0, path=paths["sup_path"])
    assert suppression.is_suppressed(" foo@example.com ", paths["sup_path"])
    suppression.suppress("+91 98765 43210", T0, path=paths["sup_path"])
    assert suppression.is_suppressed("+919876543210", paths["sup_path"])


def test_restoring_requires_a_reason_and_a_source(paths):
    suppression.suppress("x@example.com", T0, path=paths["sup_path"])
    with pytest.raises(ValueError):
        suppression.restore("x@example.com", T0, "", "", paths["sup_path"])
    suppression.restore("x@example.com", T0, "confirmed by phone", "ops-jo",
                        paths["sup_path"])
    assert not suppression.is_suppressed("x@example.com", paths["sup_path"])


def test_missing_identity_skips_only_the_suppression_check(cfg, paths):
    """An abandoned Razorpay order carries neither email nor phone.

    An earlier version returned early on a missing identity and so disabled the
    WHOLE contact gate for every abandoned order -- quiet hours included. Only
    the opt-out check may be skipped, and it is reported as skipped rather than
    silently passing.
    """
    seq = D.plan("order_X", "checkout_abandoned", 8_00_000,
                 classify("insufficient_funds", cfg), T0, cfg,
                 customer_ref=None)
    step = next(s for s in seq.steps if not s.requires_mandate)

    day = D.authorize_contact(seq, step, T0, cfg, paths["path"],
                              paths["sup_path"], paths["notif_path"])
    assert day.allowed
    assert "unchecked" in day.reason, "a skipped compliance check was not reported"

    night = datetime(2026, 8, 29, 23, 30, tzinfo=IST)
    assert not D.authorize_contact(seq, step, night, cfg, paths["path"],
                                   paths["sup_path"],
                                   paths["notif_path"]).allowed, \
        "quiet hours stopped applying when the customer identity was missing"


# ---------------------------------------------------------------------------
# one writer at a time
# ---------------------------------------------------------------------------

def test_a_second_pass_is_refused_not_queued(paths):
    """Two concurrent passes both see the same step due and both send.

    The idempotency key protects the PROVIDER from a double charge; nothing
    protected the customer from two messages. A second runner that waits and
    then fires the message the first already sent is the bug, not the fix, so
    it refuses.
    """
    with runlock.exclusive(paths["lock"], label="pass A"):
        assert runlock.is_held(paths["lock"])
        with pytest.raises(runlock.LockHeld) as e:
            with runlock.exclusive(paths["lock"], label="pass B"):
                pytest.fail("a second pass acquired the lock")
        assert "pass A" in str(e.value)
    assert not runlock.is_held(paths["lock"]), "lock survived its context"


def test_the_lock_is_released_even_when_the_pass_raises(paths):
    with pytest.raises(RuntimeError):
        with runlock.exclusive(paths["lock"]):
            raise RuntimeError("pass exploded")
    assert not runlock.is_held(paths["lock"])


def test_a_stale_lock_is_reclaimed(paths):
    """A crash leaves a lock behind. Age is the right test -- a pid check is
    wrong across containers, and a lock older than any plausible pass is either
    a crash or a hang, both of which want the same answer."""
    old = datetime.now(timezone.utc) - runlock.STALE_AFTER - timedelta(minutes=1)
    paths["lock"].parent.mkdir(parents=True, exist_ok=True)
    paths["lock"].write_text(
        f'{{"pid": 999999, "at": "{old.isoformat()}", "label": "dead"}}',
        encoding="utf-8")
    with runlock.exclusive(paths["lock"], label="new"):
        pass                                    # reclaimed without raising
    assert not runlock.is_held(paths["lock"])


# ---------------------------------------------------------------------------
# per-customer daily cap and duplicate suppression
# ---------------------------------------------------------------------------

def test_per_customer_daily_cap_is_wired(cfg, paths):
    """`max_contacts_per_customer_per_day` sat in config and was read by
    nothing, because there was no per-customer contact history to count.

    The per-reference ceiling counts messages about ONE order. A customer with
    six abandoned checkouts could clear it six times in an afternoon and be
    messaged eighteen times while every declared limit reported compliance.
    """
    cap = int(cfg["compliance"]["max_contacts_per_customer_per_day"])
    items = _items(cap + 3, customer="one@example.com")
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"], notif_path=paths["notif_path"],
             policy=_policy())
    res = run_pass(items, T0 + timedelta(hours=2), cfg, dry_run=True,
                   path=paths["path"], sup_path=paths["sup_path"],
                   notif_path=paths["notif_path"], policy=_policy())
    assert len(res.advanced) == cap, (
        f"{len(res.advanced)} contacts made to one customer; cap is {cap}")
    assert any("cap is" in d["reason"] for d in res.deferred)


def test_the_daily_cap_defers_rather_than_ending_the_campaign(cfg, paths):
    """Hitting a daily ceiling is a timing fact, not a terminal one. Ending the
    sequence would let a busy day permanently cancel a recovery."""
    items = _items(4, customer="one@example.com")
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"], notif_path=paths["notif_path"],
             policy=_policy())
    res = run_pass(items, T0 + timedelta(hours=2), cfg, dry_run=True,
                   path=paths["path"], sup_path=paths["sup_path"],
                   notif_path=paths["notif_path"], policy=_policy())
    assert res.deferred, "the daily cap did not defer anything"
    assert not any(s["reason"] == D.STOP_CONTACT_CEILING for s in res.stopped)


def test_one_rung_is_never_messaged_twice(cfg, paths):
    """A replayed webhook, an overlapping cron or a retried pass must not send
    the same rung's message again. The idempotency key stops the PROVIDER
    double-charging; this stops the CUSTOMER being messaged twice."""
    items = _items(1, customer="dup@example.com")
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"], notif_path=paths["notif_path"],
             policy=_policy())
    t = T0 + timedelta(hours=2)
    first = run_pass(items, t, cfg, dry_run=True, path=paths["path"],
                     sup_path=paths["sup_path"],
                     notif_path=paths["notif_path"], policy=_policy())
    assert len(first.advanced) == 1

    # Same clock, same ledger: a duplicate pass.
    again = run_pass(items, t, cfg, dry_run=True, path=paths["path"],
                     sup_path=paths["sup_path"],
                     notif_path=paths["notif_path"], policy=_policy())
    assert not again.advanced, "the same rung was messaged twice"


def test_a_declined_contact_is_still_recorded(cfg, paths):
    """A message we chose not to send is a fact about this customer, and it is
    the fact a complaint investigation needs."""
    suppression.suppress("s@example.com", T0, "replied STOP",
                         path=paths["sup_path"])
    items = _items(1, customer="s@example.com")
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"], notif_path=paths["notif_path"],
             policy=_policy())
    run_pass(items, T0 + timedelta(hours=2), cfg, dry_run=True,
             path=paths["path"], sup_path=paths["sup_path"],
             notif_path=paths["notif_path"], policy=_policy())
    rows = notify.read(paths["notif_path"])
    assert any(r["status"] == notify.SUPPRESSED for r in rows)


def test_notification_stops_at_requested_on_razorpay(cfg, paths):
    """Razorpay sends the message itself and reports no per-message delivery.

    Promoting REQUESTED to SENT because the API returned 200 would invent a
    fact, and a system that does that reports a bounce rate of zero forever.
    """
    items = _items(1, customer="r@example.com")
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"], notif_path=paths["notif_path"],
             policy=_policy())
    run_pass(items, T0 + timedelta(hours=2), cfg, dry_run=True,
             path=paths["path"], sup_path=paths["sup_path"],
             notif_path=paths["notif_path"], policy=_policy())
    statuses = {r["status"] for r in notify.read(paths["notif_path"])}
    assert notify.DELIVERED not in statuses
    assert notify.REQUESTED in statuses


def test_a_crashed_attempt_does_not_stall_the_sequence_forever(cfg, paths):
    """Duplicate suppression must be time-bounded, or one crash freezes a
    campaign permanently.

    The window it protects is narrow: we recorded that a message was requested,
    then died before recording the outcome. Once an outcome IS recorded the
    attempt counter advances and the check cannot fire anyway -- so an unbounded
    version protects nothing extra, and after a crash every future pass skips
    the rung, the sequence never advances, and the campaign is stuck at DUE
    until someone reads the ledger by hand.

    This is not hypothetical: it froze the 14-day projection on day zero,
    because a previous run's notification records made every rung look
    already-sent.
    """
    items = _items(1, customer="crash@example.com")
    run_pass(items, T0, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"], notif_path=paths["notif_path"],
             policy=_policy())
    ref = items[0].reference

    # A message requested, and then nothing -- the process died mid-attempt.
    notify.record(ref, "seq", 0, "none", notify.REQUESTED, T0,
                  customer_ref="crash@example.com", path=paths["notif_path"])

    blocked = run_pass(items, T0 + timedelta(hours=2), cfg, dry_run=True,
                       path=paths["path"], sup_path=paths["sup_path"],
                       notif_path=paths["notif_path"], policy=_policy())
    assert not blocked.advanced, "the duplicate check should hold briefly"

    gap = float(cfg["compliance"]["min_hours_between_attempts"])
    later = run_pass(items, T0 + timedelta(hours=gap + 2), cfg, dry_run=True,
                     path=paths["path"], sup_path=paths["sup_path"],
                     notif_path=paths["notif_path"], policy=_policy())
    assert later.advanced, (
        "the sequence never recovered from a crashed attempt; duplicate "
        "suppression is unbounded and has stalled it permanently")
