"""Tests for the simulated batch.

The batch exists to demonstrate the engine over hundreds of cases, so these
tests are mostly about the things that would make such a demonstration a lie:
a simulator that touches production data, one that cannot be reproduced, one
whose gateway can do things the real account cannot, or one whose customers
quietly get a second chance at being recoverable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from src.recovery import ledger as L
from src.sim import book as B
from src.sim import customer as C
from src.sim import run as R
from src.sim.gateway import SimGateway

IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 9, 1, 9, 0, tzinfo=IST)

LIVE = Path("data/live")


@pytest.fixture
def out(tmp_path):
    """Every batch in these tests writes to its own directory.

    They used to share `data/sim/`, and since every run begins by truncating
    those files, `pytest` quietly replaced a two-hundred-case result with a
    twelve-case fixture under the same filename. Reading the ledger afterwards
    gave a wrong answer that looked entirely plausible.
    """
    return tmp_path / "sim"


def _tiny(out, **kw):
    return R.run(n=12, days=3, seed=5, start=START, out_dir=out, **kw)


# ---------------------------------------------------------------------------
# the boundary with production
# ---------------------------------------------------------------------------

def test_the_batch_never_writes_to_live_data(out):
    """The single most important property in this package.

    A simulated batch writes a lot of events that look exactly like real ones.
    If any of them reached `data/live/`, the measured Rs 28,433 on the real
    account would be contaminated by invented recoveries and there would be no
    way to tell them apart afterwards -- the ledger is append-only by design.
    """
    before = {p: p.stat().st_mtime_ns for p in LIVE.glob("*.jsonl")} if LIVE.exists() else {}
    _tiny(out)
    after = {p: p.stat().st_mtime_ns for p in LIVE.glob("*.jsonl")} if LIVE.exists() else {}
    assert before == after, "the simulated batch modified live data"
    assert R.LEDGER.parent != LIVE


def test_every_path_the_default_batch_writes_is_under_data_sim():
    for p in R.DEFAULT.all + (R.DEFAULT.lock,):
        assert p.parts[:2] == ("data", "sim"), p


def test_a_batch_is_reproducible_from_its_seed(tmp_path):
    """Reported without a seed, a simulated number is unfalsifiable."""
    a = _tiny(tmp_path / "a")
    won_a = L.recovered_paise(a.paths.ledger)
    b = _tiny(tmp_path / "b")
    won_b = L.recovered_paise(b.paths.ledger)
    assert won_a == won_b
    assert a.contacts == b.contacts
    assert [i.reference for i in a.items] == [i.reference for i in b.items]


def test_a_different_seed_is_a_different_book(tmp_path):
    a = R.run(n=12, days=2, seed=1, start=START, out_dir=tmp_path / "a")
    b = R.run(n=12, days=2, seed=2, start=START, out_dir=tmp_path / "b")
    assert [i.reference for i in a.items] != [i.reference for i in b.items]


# ---------------------------------------------------------------------------
# the gateway
# ---------------------------------------------------------------------------

def test_the_simulated_gateway_cannot_do_what_the_real_account_cannot():
    """The temptation this guards against.

    Letting the simulated gateway charge a saved instrument would make the
    ladder's silent rungs convert, and the whole mandate argument in
    docs/RECOVERY_RATE.md would evaporate into a much better-looking number.
    """
    caps = SimGateway().capabilities()
    assert not caps.can_charge_saved_instrument
    assert not caps.can_restrict_methods


def test_a_link_is_dated_by_the_simulated_clock_not_the_wall_clock():
    """An audit trail whose links are created after the recoveries they
    produced is worse than no audit trail -- the same reason `--as-of` is
    refused with `--execute` on the live ledger."""
    gw = SimGateway(clock=START)
    art = gw.create_recovery(500000, "x")
    assert gw.links[art.reference].created_at == START


def test_the_gateway_does_not_decide_who_pays():
    """It creates links and reports status. Nothing more.

    Keeping the decision in the customer model is what stops the gateway
    quietly becoming the simulation.
    """
    gw = SimGateway(clock=START)
    art = gw.create_recovery(500000, "x")
    assert not gw.fetch_recovery(art.reference).paid
    gw.mark_paid(art.reference, START)
    assert gw.fetch_recovery(art.reference).paid


def test_paying_twice_does_not_double_the_money():
    gw = SimGateway(clock=START)
    art = gw.create_recovery(500000, "x")
    gw.mark_paid(art.reference, START)
    gw.mark_paid(art.reference, START + timedelta(days=1), amount_paise=999)
    got = gw.fetch_recovery(art.reference)
    assert got.amount_paid_paise == 500000


# ---------------------------------------------------------------------------
# the customer
# ---------------------------------------------------------------------------

def test_an_unrecoverable_customer_never_converts_however_often_contacted():
    """The per-class ceiling is what stops a long ladder recovering cases no
    ladder can. Redrawing recoverability per attempt would quietly let the
    schedule outrun the physics."""
    rng = np.random.default_rng(0)
    p = C.Persona("r", "SOFT_FUNDS", recoverable=False, will_keep_promise=True)
    assert C.p_convert(0.9, p, 100000) == 0.0
    outcomes = {C.react(p, START, 100000, 0.9, rng).kind for _ in range(200)}
    assert C.PAYS not in outcomes


def test_a_bigger_invoice_converts_no_better_than_a_small_one():
    p = C.Persona("r", "OVERDUE", True, True)
    small = C.p_convert(0.25, p, 500_00)
    large = C.p_convert(0.25, p, 900_000_00)
    assert large < small


def test_abandonment_is_the_least_recoverable_stream():
    """Every published band puts abandoned-cart recovery far below failed
    payments, and a model that forgot it would be the flattering kind."""
    assert C.CEILING["ABANDONED"] < C.CEILING["OVERDUE"]
    assert C.CEILING["ABANDONED"] < C.CEILING["SOFT_FUNDS"]


def test_opting_out_is_checked_before_paying():
    """A customer annoyed enough to leave does not first pay and then leave.
    Checking conversion first would suppress the opt-out rate on exactly the
    cases that convert best."""
    rng = np.random.default_rng(3)
    p = C.Persona("r", "ABANDONED", True, True)
    # Certain opt-out, certain conversion: only the order decides the answer.
    old = C.OPT_OUT_BASE["ABANDONED"]
    C.OPT_OUT_BASE["ABANDONED"] = 1.0
    try:
        assert C.react(p, START, 100000, 0.99, rng).kind == C.OPTS_OUT
    finally:
        C.OPT_OUT_BASE["ABANDONED"] = old


def test_only_receivables_negotiate():
    assert "OVERDUE" in C.PROMISE_RATE
    assert C.PROMISE_RATE.get("ABANDONED", 0) == 0


# ---------------------------------------------------------------------------
# the book
# ---------------------------------------------------------------------------

def test_every_generated_identifier_is_unmistakably_synthetic():
    """A simulated case that leaked into a sending path must have no inbox at
    the other end. `.invalid` is reserved by RFC 2606 precisely for this."""
    for it in B.generate(60, START, seed=1):
        assert it.reference.startswith("sim_")
        ref = (it.detail or {}).get("customer_ref") or ""
        assert ref.endswith("@sim.invalid") or ref.startswith("+9199999"), ref


def test_generated_failures_classify_through_the_real_taxonomy():
    """The reasons are Razorpay's own strings, so an unmapped one would show up
    as UNKNOWN rather than being quietly assigned a schedule."""
    from src.recovery.campaign import classify_item
    from src.recovery.declines import load_declines
    cfg = load_declines()
    seen = set()
    for it in B.generate(300, START, seed=2):
        cls = classify_item(it, cfg)
        seen.add(cls.decline_class)
        if it.kind == "payment_failure":
            assert cls.mapped, f"{it.detail.get('error_reason')} is unmapped"
    assert {"ABANDONED", "OVERDUE", "SOFT_FUNDS"} <= seen


def test_the_book_arrives_over_time_rather_than_all_at_once():
    """With every case dated before the start, every ladder is phase-locked to
    one hour of the day and the quiet-hours guard is never asked a question.
    It reported zero deferrals and looked broken."""
    spread = B.generate(120, START, seed=4, arrival_days=10)
    later = [i for i in spread if i.created_at > START]
    assert later, "nothing arrives during the run window"
    hours = {i.created_at.hour for i in spread}
    assert len(hours) > 6, hours


# ---------------------------------------------------------------------------
# the whole thing
# ---------------------------------------------------------------------------

def test_a_small_batch_holds_every_compliance_invariant(out):
    """The integration test. Runs the real engine over a real ladder and
    checks the properties the batch exists to demonstrate."""
    from scripts import simulate_batch as S
    from src.recovery.channels import load_workflows
    from src.recovery.declines import load_declines
    res = R.run(n=30, days=6, seed=11, start=START, out_dir=out)
    checks = [
        S.check_no_contact_after_opt_out(res),
        S.check_quiet_hours(res, load_workflows()),
        S.check_contact_ceiling(res, load_declines()),
        S.check_write_ahead(res),
        S.check_one_success(res),
    ]
    for ok, detail in checks:
        assert ok, detail


def test_recovered_money_comes_from_the_gateway_not_from_the_simulator(out):
    """Nothing in the simulator writes a recovery.

    TWO ROUTES TO MONEY, and this test used to know only one. A link is marked
    paid on the gateway and `reconcile_links` -- production code -- discovers
    it on a later tick. A mandate re-present is synchronous: the gateway
    answers captured or declined there and then, which is exactly why a
    subscription book recovers faster than a checkout book. Both produce a
    gateway reference; neither is asserted by the simulation.
    """
    res = R.run(n=40, days=8, seed=9, start=START, out_dir=out)
    rows = L.read(res.paths.ledger)
    wins = [e for e in rows if e["event"] == L.ATTEMPT_SUCCEEDED]
    assert wins, "no recoveries in this batch; the test proves nothing"
    for w in wins:
        ref = w.get("execution_reference") or ""
        assert ref.startswith(("plink_sim_", "pay_sim_")), ref
        assert w["execution_mode"] == "SIMULATED"
    total = sum(int(w.get("amount_paise") or 0) for w in wins)
    assert total == L.recovered_paise(res.paths.ledger)


def test_only_a_case_with_a_mandate_gets_a_silent_retry(out):
    """The error this guards against, which was live for one commit.

    `fast` and `slow` -- the ordinary one-off failure ladders -- each open with
    a `requires_mandate` silent rung, not just the 7-step `mandate` schedule.
    So a gateway that could charge mandates handed a free silent retry to every
    checkout failure in the book: money the live account could not have
    recovered, which is the one thing this simulator must never do.
    """
    res = R.run(n=120, days=14, seed=4, start=START, out_dir=out)
    with_mandate = {i.reference for i in res.items
                    if (i.detail or {}).get("mandate")}
    charged = {c["reference"] for c in res.gateway.charges}
    assert charged, "no silent charges at all; the test proves nothing"
    assert charged <= with_mandate, sorted(charged - with_mandate)


def test_a_subscription_is_rebuilt_onto_the_ladder_it_was_opened_on(out):
    """`rebuild` promises replanning reproduces the same steps. For
    subscriptions it did not: the kind fell through to the reason-based
    classifier, so a 7-step mandate ladder came back as a 5-step slow one."""
    from src.recovery.campaign import rebuild
    from src.recovery.declines import load_declines
    res = R.run(n=80, days=6, seed=3, start=START, out_dir=out)
    cfg = load_declines()
    subs = [i.reference for i in res.items
            if i.kind == "subscription_failure"
            and L.events_for(i.reference, res.paths.ledger)]
    assert subs, "no subscriptions opened; the test proves nothing"
    for ref in subs[:5]:
        seq = rebuild(ref, cfg, res.paths.ledger)
        assert seq.classification.schedule == "mandate"
        assert seq.extra.get("mandate") is not None


def test_a_second_batch_cannot_start_while_one_is_running(out):
    """Two batches share fixed paths and `fresh()` truncates them, so an
    overlapping run does not interleave -- it deletes the file the other is
    reading. That is how the first full-size run of this script ended."""
    from src.recovery import runlock
    paths = R.SimPaths(out)
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    with runlock.exclusive(path=paths.lock, label="pretend-batch"):
        with pytest.raises(runlock.LockHeld):
            R.run(n=5, days=1, seed=1, start=START, out_dir=out)
