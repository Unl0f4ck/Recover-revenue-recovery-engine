"""Tests for the four workflows, the channel rules, and promise-to-pay."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.ingest.razorpay_source import RevenueAtRisk, overdue_receivables
from src.recovery import channels, dunning as D, promises
from src.recovery import workflows as W
from src.recovery.campaign import classify_item, run_pass
from src.recovery.declines import classify_receivable, load_declines

IST = timezone(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 8, 28, 12, 0, tzinfo=IST)


@pytest.fixture
def cfg():
    return load_declines()


@pytest.fixture
def wf_cfg():
    return channels.load_workflows()


@pytest.fixture
def paths(tmp_path):
    return {"path": tmp_path / "ledger.jsonl",
            "sup_path": tmp_path / "suppression.jsonl",
            "notif_path": tmp_path / "notifications.jsonl",
            "promise_path": tmp_path / "promises.jsonl"}


def _policy():
    return {"bounds": {"global_kill_switch": False},
            "batch_limits": {"max_interventions_per_run": 50,
                             "max_exposure_per_run_paise": 100_000_000}}


# ---------------------------------------------------------------------------
# the four workflows
# ---------------------------------------------------------------------------

def test_all_four_workflows_are_declared(wf_cfg):
    got = {w.key for w in W.ordered(wf_cfg)}
    assert got == {"payment_degradation", "checkout_abandonment",
                   "subscription_failure", "invoice_overdue"}


def test_each_workflow_owns_a_distinct_stream(wf_cfg):
    kinds = [w.kind for w in W.ordered(wf_cfg)]
    assert len(kinds) == len(set(kinds)), "two workflows claim the same stream"


def test_only_two_workflows_have_anything_to_diagnose(wf_cfg):
    """An abandoned cart has no fault to find and an overdue invoice has no
    fault either -- only a debtor. Saying so is more useful than pretending
    every stream runs the same machinery."""
    diag = {w.key for w in W.ordered(wf_cfg) if w.diagnoses}
    assert diag == {"payment_degradation", "subscription_failure"}


def test_each_workflow_has_a_different_ladder(cfg, wf_cfg):
    shapes = {w.key: W.ladder_shape(w, cfg, wf_cfg) for w in W.ordered(wf_cfg)}
    assert len({s["schedule"] for s in shapes.values()}) == 4


def test_only_the_mandate_ladder_leads_with_silent_rungs(cfg, wf_cfg):
    """The whole point of a mandate: retrying costs nothing and interrupts
    nobody, so the free rungs come first and the customer is troubled last."""
    for w in W.ordered(wf_cfg):
        first = W.ladder_shape(w, cfg, wf_cfg)["channels"][0]
        if w.mandate_backed:
            assert first == "silent", f"{w.key} wastes its mandate"
        else:
            assert first != "silent" or not w.mandate_backed


def test_the_mandate_ladder_is_the_longest_and_cheapest(cfg, wf_cfg):
    shapes = {w.key: W.ladder_shape(w, cfg, wf_cfg) for w in W.ordered(wf_cfg)}
    m = shapes["subscription_failure"]
    assert m["steps"] == max(s["steps"] for s in shapes.values())
    assert m["silent"] > 0
    # More attempts than anyone, yet fewer customer contacts than the
    # receivables ladder -- which is exactly what a mandate buys.
    assert m["contacts"] < shapes["invoice_overdue"]["contacts"]


def test_receivables_end_with_a_person_not_a_write_off(cfg, wf_cfg):
    """Nobody writes off a debt automatically; somebody decides to."""
    shape = W.ladder_shape(W.load(wf_cfg)["invoice_overdue"], cfg, wf_cfg)
    assert shape["ends_with"] == "human_review"


def test_readiness_names_the_blocker_per_workflow(wf_cfg):
    """One global "some of this is simulated" footnote loses the only detail a
    reviewer can act on."""
    wf = W.load(wf_cfg)

    class NoMandate:
        can_charge_saved_instrument = False

    r = W.readiness(wf["subscription_failure"], [], NoMandate())
    assert not r.ready
    assert any("Subscriptions is not enabled" in b for b in r.blockers)
    assert any("mandate" in b for b in r.blockers)

    assert W.readiness(wf["checkout_abandonment"], [], NoMandate()).ready


def test_degradation_needs_volume_before_it_claims_anything(wf_cfg):
    wf = W.load(wf_cfg)["payment_degradation"]
    thin = [RevenueAtRisk("payment_failure", f"p{i}", NOON, 9_00_000, "HDFC",
                          "card") for i in range(5)]
    r = W.readiness(wf, thin, None)
    assert not r.ready
    assert any("rate shift" in b for b in r.blockers)


# ---------------------------------------------------------------------------
# invoice overdue
# ---------------------------------------------------------------------------

def _invoice(days_ago: int, paise: int = 90_00_000, status: str = "issued",
             paid: int = 0, notes=None):
    issued = int((NOON - timedelta(days=days_ago)).timestamp())
    return {"id": f"inv_{days_ago}_{paise}", "status": status,
            "amount": paise, "amount_paid": paid, "date": issued,
            "notes": notes or {},
            "customer_details": {"name": "A", "email": "a@example.com"}}


def test_a_due_date_is_derived_from_terms_not_read_off_a_field():
    """Razorpay's `expire_by` is when the LINK stops working, which is a
    technical expiry and not a commercial one -- and it is frequently unset. A
    receivable is overdue when issue date plus agreed terms has passed."""
    got = overdue_receivables([_invoice(20)], NOON, payment_terms_days=14,
                              grace_days=2)
    assert len(got) == 1
    assert got[0].detail["days_overdue"] == 6
    assert got[0].kind == "overdue_receivable"


def test_grace_protects_someone_who_is_merely_a_little_late():
    """Invoices are routinely paid a day or two late by people who fully intend
    to pay. A reminder on the morning of day one annoys a paying customer."""
    assert overdue_receivables([_invoice(15)], NOON, 14, 2) == []
    assert overdue_receivables([_invoice(20)], NOON, 14, 2) != []


def test_a_partly_paid_invoice_chases_only_the_balance():
    got = overdue_receivables([_invoice(30, 90_00_000, paid=40_00_000)],
                              NOON, 14, 2)
    assert got[0].amount_paise == 50_00_000


def test_paid_and_cancelled_invoices_are_not_chased():
    for status in ("paid", "cancelled", "draft"):
        assert overdue_receivables([_invoice(30, status=status)],
                                   NOON, 14, 2) == []


def test_our_own_invoices_are_never_chased():
    from src.execution.razorpay import RECOVERY_TAG
    assert overdue_receivables([_invoice(30, notes={"source": RECOVERY_TAG})],
                               NOON, 14, 2) == []


def test_receivables_classify_as_their_own_thing(cfg):
    """Forcing an overdue invoice through a failure taxonomy would show an
    operator a reason that is not the true one."""
    c = classify_receivable(cfg)
    assert c.decline_class == "OVERDUE"
    assert c.schedule == "overdue"
    item = overdue_receivables([_invoice(30)], NOON, 14, 2)[0]
    assert classify_item(item, cfg).decline_class == "OVERDUE"


# ---------------------------------------------------------------------------
# channels: voice is regulated differently
# ---------------------------------------------------------------------------

def test_voice_has_a_narrower_window_than_messaging(wf_cfg):
    """TRAI restricts commercial calls to 09:00-21:00, tighter than our own
    08:00-22:00 messaging window at BOTH ends. One global rule would either
    over-restrict SMS or under-restrict calls -- and under-restricting calls is
    a regulatory breach, not a discourtesy."""
    voice = channels.get("voice", wf_cfg)
    sms = channels.get("payment_link", wf_cfg)
    assert voice.hours_ist[0] > sms.hours_ist[0]
    assert voice.hours_ist[1] < sms.hours_ist[1]
    assert voice.respects_dnd


@pytest.mark.parametrize("hour,ok", [(8, False), (9, True), (20, True),
                                     (21, False), (23, False)])
def test_voice_obeys_its_own_window(wf_cfg, hour, ok):
    voice = channels.get("voice", wf_cfg)
    when = datetime(2026, 8, 28, hour, 30, tzinfo=IST)
    assert channels.permitted_now(voice, when) is ok


def test_voice_is_reserved_for_real_money(wf_cfg):
    voice = channels.get("voice", wf_cfg)
    small = channels.authorize(voice, NOON, 10_00_000, prior_contacts=2)
    assert not small.allowed and "reserved" in small.reason
    big = channels.authorize(voice, NOON, 90_00_000, prior_contacts=2)
    assert big.allowed


def test_voice_never_comes_first(wf_cfg):
    """A call instead of a text, rather than after one, is not escalation."""
    voice = channels.get("voice", wf_cfg)
    d = channels.authorize(voice, NOON, 90_00_000, prior_contacts=0)
    assert not d.allowed
    assert "softer" in d.reason


def test_an_unavailable_channel_still_passes_through_every_other_check(wf_cfg):
    """Availability is checked LAST on purpose. If it short-circuited, an
    unconnected channel would skip the checks that make it safe, and turning it
    on later would enable a path nothing had ever tested."""
    voice = channels.get("voice", wf_cfg)
    assert voice.executable is False
    night = datetime(2026, 8, 28, 23, 30, tzinfo=IST)
    d = channels.authorize(voice, night, 90_00_000, prior_contacts=2)
    assert not d.allowed, "an unconnected channel skipped its legal window"


def test_an_unavailable_channel_is_allowed_but_marked(wf_cfg):
    voice = channels.get("voice", wf_cfg)
    d = channels.authorize(voice, NOON, 90_00_000, prior_contacts=2)
    assert d.allowed and not d.executable
    assert "telephony" in d.reason.lower()


def test_a_call_costs_far_more_than_a_message(wf_cfg):
    assert (channels.get("voice", wf_cfg).cost_paise
            > 10 * channels.get("payment_link", wf_cfg).cost_paise)


# ---------------------------------------------------------------------------
# promise to pay
# ---------------------------------------------------------------------------

def _pcfg(**over):
    base = {"promise_to_pay": {"enabled": True, "grace_days": 1,
                               "max_horizon_days": 30, "max_broken": 2}}
    base["promise_to_pay"].update(over)
    return base


def test_a_promise_pauses_the_ladder(paths):
    promises.record("inv_1", NOON + timedelta(days=5), NOON, 90_00_000,
                    cfg=_pcfg(), path=paths["promise_path"])
    held, why = promises.holds("inv_1", NOON + timedelta(days=1), _pcfg(),
                               paths["promise_path"])
    assert held and "promised to pay" in why


def test_the_pause_lifts_once_the_date_passes(paths):
    promises.record("inv_1", NOON + timedelta(days=3), NOON, cfg=_pcfg(),
                    path=paths["promise_path"])
    later = NOON + timedelta(days=5)
    assert not promises.holds("inv_1", later, _pcfg(),
                              paths["promise_path"])[0]


def test_a_promise_cannot_park_a_debt_forever(paths):
    """Without a ceiling a debtor promises an ever-later date and every promise
    looks like progress in the report while nothing is collected."""
    with pytest.raises(promises.PromiseRefused):
        promises.record("inv_1", NOON + timedelta(days=90), NOON,
                        cfg=_pcfg(), path=paths["promise_path"])


def test_a_promise_must_name_a_future_date(paths):
    with pytest.raises(promises.PromiseRefused):
        promises.record("inv_1", NOON - timedelta(days=1), NOON,
                        cfg=_pcfg(), path=paths["promise_path"])


def test_a_promise_is_kept_by_payment_not_by_the_calendar(paths):
    """A date passing proves nothing either way."""
    promises.record("inv_1", NOON + timedelta(days=2), NOON, cfg=_pcfg(),
                    path=paths["promise_path"])
    after = NOON + timedelta(days=4)
    assert promises.settle("inv_1", after, paid=True, cfg=_pcfg(),
                           path=paths["promise_path"]) == promises.KEPT


def test_a_broken_promise_is_counted(paths):
    promises.record("inv_1", NOON + timedelta(days=2), NOON, cfg=_pcfg(),
                    path=paths["promise_path"])
    promises.settle("inv_1", NOON + timedelta(days=4), paid=False,
                    cfg=_pcfg(), path=paths["promise_path"])
    assert promises.count_broken("inv_1", paths["promise_path"]) == 1


def test_after_enough_broken_promises_a_person_must_agree_the_next(paths):
    t = NOON
    for _ in range(2):
        promises.record("inv_1", t + timedelta(days=2), t, cfg=_pcfg(),
                        path=paths["promise_path"])
        t = t + timedelta(days=4)
        promises.settle("inv_1", t, paid=False, cfg=_pcfg(),
                        path=paths["promise_path"])
    with pytest.raises(promises.PromiseRefused) as e:
        promises.record("inv_1", t + timedelta(days=2), t, cfg=_pcfg(),
                        path=paths["promise_path"])
    assert "person should agree" in str(e.value)


def test_a_promise_stops_silent_retries_too(cfg, wf_cfg, paths):
    """Someone who committed to a date should not be surprise-debited on
    Wednesday either. A charge is worse than a text, not better."""
    seq = D.plan("inv_9", "overdue_receivable", 90_00_000,
                 classify_receivable(cfg), NOON, cfg,
                 customer_ref="a@example.com")
    silent = D.Step(0, NOON, "silent", "same", requires_mandate=True)
    promises.record("inv_9", NOON + timedelta(days=5), NOON, cfg=_pcfg(),
                    path=paths["promise_path"])
    d = D.authorize_contact(seq, silent, NOON + timedelta(days=1), cfg,
                            paths["path"], paths["sup_path"],
                            paths["notif_path"], _pcfg(),
                            paths["promise_path"])
    assert not d.allowed and d.paused


def test_a_paused_case_is_not_a_stopped_case(cfg, wf_cfg, paths):
    """A promise is neither a deferral nor an ending: the ladder waits and
    resumes from where it stood."""
    items = [RevenueAtRisk("overdue_receivable", "inv_p", NOON, 90_00_000,
                           "UNATTRIBUTED", None,
                           {"customer_ref": "a@example.com"})]
    run_pass(items, NOON, cfg, dry_run=True, path=paths["path"],
             sup_path=paths["sup_path"], notif_path=paths["notif_path"],
             promise_path=paths["promise_path"], wf_cfg=_pcfg(),
             policy=_policy())
    # The overdue ladder's second rung falls at +72h, so the pause is only
    # exercised at a moment when a step is actually due. Testing it an hour
    # after the first rung would pass for the wrong reason -- nothing was due,
    # so nothing was blocked.
    promises.record("inv_p", NOON + timedelta(days=9), NOON, cfg=_pcfg(),
                    path=paths["promise_path"])
    when = NOON + timedelta(days=4)
    res = run_pass(items, when, cfg, dry_run=True,
                   path=paths["path"], sup_path=paths["sup_path"],
                   notif_path=paths["notif_path"],
                   promise_path=paths["promise_path"], wf_cfg=_pcfg(),
                   policy=_policy())
    assert not res.advanced
    assert not res.stopped, "a promise ended the campaign instead of pausing it"
    assert any("promised" in d["reason"] for d in res.deferred)


# ---------------------------------------------------------------------------
# what chasing actually costs
# ---------------------------------------------------------------------------

def test_the_floor_is_derived_not_chosen(cfg, wf_cfg):
    """It used to be four round numbers -- Rs 200 / 500 / 1,000 / 1,500 -- at
    2x, 5x, 12x and 750x their own break-even. Four unrelated figures with prose
    written after the fact, and nothing derived any of them.

    The floor is now (cash + contacts x price-of-an-interruption) / p, so the
    only judgement left is what one interruption is worth. That is a far better
    thing to expose: it has a unit, it is comparable across workflows, and a
    merchant can disagree with it specifically.
    """
    from src.recovery import economics as E
    for w in W.ordered(wf_cfg):
        e = E.for_workflow(w, cfg, wf_cfg)
        expected = int((e.total_cost_paise + e.contacts *
                        e.nuisance_per_contact_paise) / e.recovery_probability)
        assert e.policy_floor_paise == expected, (
            f"{w.key}: the floor is not what the model produces, so a number "
            f"has been hard-coded somewhere")
        assert e.nuisance_per_contact_paise > 0, (
            f"{w.key}: no price set for interrupting someone, so its floor "
            f"collapses to the cash break-even")


def test_adding_a_rung_raises_the_floor(cfg, wf_cfg):
    """The property a hard-coded floor cannot have. Ask the customer for more
    patience and the amount that justifies asking must go up."""
    from src.recovery import economics as E
    contacting = set(cfg["compliance"]["contacting_channels"])
    steps = cfg["schedules"]["abandoned"]["steps"]
    base = E.economics_for("x", steps, 0.3, 15000, "", contacting, wf_cfg)
    longer = E.economics_for(
        "x", steps + [{"channel": "payment_link", "after_hours": 400}],
        0.3, 15000, "", contacting, wf_cfg)
    assert longer.policy_floor_paise > base.policy_floor_paise


def test_at_zero_nuisance_the_floor_is_just_the_cash_break_even(cfg, wf_cfg):
    """Everything above the cash floor IS the value placed on not bothering
    people. Saying so makes the judgement visible rather than implied."""
    from src.recovery import economics as E
    contacting = set(cfg["compliance"]["contacting_channels"])
    steps = cfg["schedules"]["overdue"]["steps"]
    free = E.economics_for("x", steps, 0.3, 0, "", contacting, wf_cfg)
    assert free.policy_floor_paise == free.cash_floor_paise


def test_the_floor_is_not_a_break_even_and_says_so(cfg, wf_cfg):
    """The floor was Rs 5,000, justified as the point below which "a recovery
    attempt costs more than it can bring back". Nobody had checked.

    An SMS costs 20 paise. Three cost 60. At a 30% recovery rate the true
    break-even is tens of rupees, so the stated justification was wrong by more
    than two orders of magnitude. The floor is a POLICY CHOICE -- a contact
    spends a customer's tolerance and the merchant's sender reputation, neither
    of which scales with the amount -- and it must be labelled as one.
    """
    from src.recovery import economics as E
    for w in W.ordered(wf_cfg):
        e = E.for_workflow(w, cfg, wf_cfg)
        assert e.economic_floor_paise < 20_000, (
            f"{w.key}: break-even of Rs {e.economic_floor_paise/100:,.0f} is "
            f"implausibly high for messaging that costs "
            f"{e.ladder_cost_paise} paise")
        assert e.policy_floor_paise > e.economic_floor_paise, (
            f"{w.key}: the chosen floor is below break-even, which would mean "
            f"knowingly chasing cases that lose money")
        assert e.policy_reason and "no reason recorded" not in e.policy_reason, (
            f"{w.key}: a floor without a stated reason is indistinguishable "
            f"from a calculation, which is how Rs 5,000 happened")


def test_every_floor_is_far_above_break_even(cfg, wf_cfg):
    """Not a bug -- the point. Stating the gap makes the judgement visible."""
    from src.recovery import economics as E
    for w in W.ordered(wf_cfg):
        assert E.for_workflow(w, cfg, wf_cfg).gap >= 2


def test_the_floor_differs_by_workflow(cfg, wf_cfg):
    """One floor across all four was wrong in both directions at once: too high
    for an invoice the customer already owes, too low for an unsolicited nudge
    about a small cart."""
    from src.recovery import economics as E
    floors = {w.key: E.for_workflow(w, cfg, wf_cfg).policy_floor_paise
              for w in W.ordered(wf_cfg)}
    assert floors["checkout_abandonment"] > floors["invoice_overdue"], (
        "an unsolicited nudge should need MORE justification than a reminder "
        "about money already owed")
    assert floors["subscription_failure"] == min(floors.values()), (
        "the mandate ladder opens with free silent retries, so it has almost "
        "nothing to weigh against recovery")


def test_human_review_is_the_cost_that_actually_matters(cfg, wf_cfg):
    """Messaging is nearly free; an operator's attention is not. Counting it
    honestly is what shows that even WITH it the break-even stays low."""
    from src.recovery import economics as E
    with_human = E.for_workflow(W.load(wf_cfg)["invoice_overdue"], cfg, wf_cfg)
    without = E.for_workflow(W.load(wf_cfg)["checkout_abandonment"], cfg, wf_cfg)
    assert with_human.human_cost_paise > with_human.ladder_cost_paise
    assert without.human_cost_paise == 0, \
        "a ladder ending in write-off should not charge for an operator"


def test_the_floor_actually_applied_is_the_workflow_one(cfg, wf_cfg):
    from src.recovery import economics as E
    small = E.floor_for("overdue_receivable", cfg, wf_cfg) + 1
    ok, why = D.may_open(small, cfg, "overdue_receivable", wf_cfg)
    assert ok, why
    # The same amount against the abandonment floor is declined.
    assert not D.may_open(small, cfg, "checkout_abandoned", wf_cfg)[0]


def test_an_invoice_and_its_order_are_not_counted_twice():
    """Every Razorpay invoice creates a backing order, and that order is
    unpaid by definition while the invoice is unpaid.

    Without suppression the same money appears in two streams -- once as an
    overdue receivable, once as an abandoned checkout -- and the book inflates
    by the entire receivables balance. It did, by Rs 2,96,050 across 16 orders.
    The code already guarded this for payment links; invoices were missed.
    """
    from src.ingest.razorpay_source import abandoned_checkouts
    stale = int((NOON - timedelta(hours=3)).timestamp())
    order = {"id": "order_INV", "status": "created", "created_at": stale,
             "amount": 90_00_000, "amount_paid": 0}
    other = {"id": "order_PLAIN", "status": "created", "created_at": stale,
             "amount": 50_00_000, "amount_paid": 0}
    inv = {"id": "inv_1", "order_id": "order_INV", "status": "issued"}

    both = abandoned_checkouts([order, other], [], [], now=NOON)
    assert {r.reference for r in both} == {"order_INV", "order_PLAIN"}

    deduped = abandoned_checkouts([order, other], [], [], now=NOON,
                                  invoices=[inv])
    assert {r.reference for r in deduped} == {"order_PLAIN"}


def test_a_cancelled_invoice_leaves_no_order_to_chase():
    """A merchant who cancelled an invoice has said they no longer want the
    money. Chasing the ghost of it is worse than missing it."""
    from src.ingest.razorpay_source import abandoned_checkouts
    stale = int((NOON - timedelta(hours=3)).timestamp())
    order = {"id": "order_X", "status": "created", "created_at": stale,
             "amount": 90_00_000, "amount_paid": 0}
    cancelled = {"id": "inv_c", "order_id": "order_X", "status": "cancelled"}
    assert abandoned_checkouts([order], [], [], now=NOON,
                               invoices=[cancelled]) == []


# ---------------------------------------------------------------------------
# the degradation loop RUNS
# ---------------------------------------------------------------------------

def test_the_degradation_loop_always_runs_and_reports(cfg):
    """"Not enough traffic" is a RESULT, not a blocker.

    This workflow used to report "BLOCKED: needs 30 payments in one segment",
    which reads as broken. A monitoring loop that runs, looks and finds nothing
    is working exactly as intended -- the alternative, one that fires on four
    payments, is the false-intervention defect this project already fixed once.
    """
    from src.recovery.degradation import scan, summarise
    from src.ingest.razorpay_source import RealPayment

    quiet = [RealPayment(f"pay_{i}", NOON, 10_000, "captured", "card",
                         "HDFC", None, None, None, None, None, None, None)
             for i in range(40)]
    s = summarise(scan(quiet, NOON))
    assert s["payments_seen"] == 40
    assert s["incident"] is False
    assert "Nothing to fix" in s["conclusion"]
    assert s["conclusion"], "a quiet result must still say something"


def test_a_thin_account_says_so_rather_than_claiming_health(cfg):
    """A quiet result on six payments means "cannot tell", not "definitely
    fine", and conflating those is how a monitoring system lies by omission."""
    from src.recovery.degradation import scan, summarise
    from src.ingest.razorpay_source import RealPayment
    few = [RealPayment(f"pay_{i}", NOON, 10_000, "captured", "card", "HDFC",
                       None, None, None, None, None, None, None)
           for i in range(4)]
    s = summarise(scan(few, NOON))
    assert s["thin"] is True
    assert "not enough traffic to say" in s["thin_note"]


def test_a_real_degradation_is_detected_diagnosed_and_acted_on(cfg):
    """The whole loop, end to end, on a cell that genuinely goes bad.

    Same detector and same attribution ladder the 240-scenario frozen
    experiment scored -- `to_windows` is the seam that lets identical machinery
    read the API instead of the simulator.
    """
    from src.recovery.degradation import scan, summarise
    from src.ingest.razorpay_source import RealPayment

    ps = []
    # A healthy background across two issuers.
    for i in range(60):
        ps.append(RealPayment(f"ok_{i}", NOON + timedelta(seconds=i * 30),
                              10_000, "captured", "card", "ICICI",
                              None, None, None, None, None, None, None))
    # One issuer falls over: sustained failures on the same rail.
    for i in range(40):
        ps.append(RealPayment(
            f"bad_{i}", NOON + timedelta(seconds=i * 30), 50_000, "failed",
            "card", "HDFC", None, None, "BAD_REQUEST_ERROR", "issuer_bank",
            "payment_authorization", "bank_technical_error", None))

    s = summarise(scan(ps, NOON))
    assert s["incident"] is True, s["conclusion"]
    assert s["alerting"], "an incident with no alerting cell"
    assert s["level"] in ("L1", "L2"), s
    assert s["action"], "detected and diagnosed but chose no action"
    assert s["at_risk_paise"] > 0
