"""The two loops that could not run at all, and now can.

Payment degradation and subscription failure were both BLOCKED — for entirely
different reasons, neither of them a missing feature:

    degradation    starved. The detector needs ~30 attempts in a segment to
                   tell a rate shift from noise; the live account's busiest
                   cell has one.
    subscription   gated. `/subscriptions` and `/plans` return 401 on a key
                   that reads `/payments` and `/customers` fine.

Both blockers are still true and still reported. What changed is that the loops
now have a data path that exercises them, so the code between "we found
something" and "we did something about it" is no longer unreachable — and three
real bugs were living in exactly that unreachable stretch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.recovery.degradation import scan
from src.sim import traffic as T

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 1, 14, 0, tzinfo=IST)


# ---------------------------------------------------------------------------
# payment degradation
# ---------------------------------------------------------------------------

def test_the_detector_finds_an_incident_it_was_never_told_about():
    """The generator returns ground truth so a test can grade the answer. The
    scan never sees it: an incident payment is an ordinary failed payment with
    an ordinary error signature, and the only thing marking the window is that
    there are more of them."""
    pays, truth = T.generate(minutes=240, per_minute=9.0, seed=7, now=NOW)
    res = scan(pays)
    assert res.incident
    found = {(c.segment, c.method) for c in res.alerting_cells}
    assert (truth.segment, truth.method) in found


def test_it_does_not_cry_wolf_on_healthy_traffic():
    """The expensive false positive. Rerouting traffic away from a bank that is
    working is worse than missing a real incident, because it happens on every
    quiet day rather than on the rare bad one."""
    for seed in (1, 2, 3, 4):
        pays, truth = T.generate(minutes=240, per_minute=9.0, seed=seed,
                                 now=NOW, incident=False)
        assert truth is None
        res = scan(pays)
        assert not res.incident, f"seed {seed}: {res.alerting_cells}"


def test_an_issuer_the_topology_has_never_heard_of_does_not_crash():
    """`config/topology.yaml` holds the abstract ISSUER_A..H of the frozen
    experiment. Razorpay reports HDFC, SBIN and the rest, so the ladder indexed
    `routing[issuer]` and raised KeyError on every real issuer alive. The loop
    was guaranteed to crash the first time a real book gave it enough volume to
    reach diagnosis, and only escaped it because the live account is too quiet
    to detect anything."""
    pays, truth = T.generate(minutes=240, per_minute=9.0, seed=7, now=NOW)
    assert truth.segment not in ("ISSUER_A", "ISSUER_B")
    res = scan(pays)                      # must not raise
    assert res.diagnosis is not None


def test_the_mechanism_comes_from_the_incident_not_from_the_background():
    """The quiet failure this guards against.

    `dominant_source` was the mode over EVERY failed payment on the account. On
    a book failing at a healthy 9% with one cell blown out to 62%, ordinary
    customer declines outnumber the incident's failures about eight to one --
    so the source came back `customer`, no mechanism family matched, and the
    loop chose NO_ACTION. Detection worked, localisation worked, and nothing
    happened.
    """
    pays, truth = T.generate(minutes=240, per_minute=9.0, seed=7, now=NOW,
                             incident_source="issuer_bank")
    res = scan(pays)
    assert res.diagnosis.evidence["dominant_source"] == "issuer_bank"
    assert res.diagnosis.mechanism_family == "issuer_degradation"


def test_an_issuer_outage_is_answered_by_a_reroute_not_by_a_message():
    """An incident is fixed by moving traffic, not by dunning the customers who
    happened to be caught in it. `prefers_rail_change` in the workflow config
    says so; this checks the policy actually does it."""
    from src.policy import Action
    pays, _ = T.generate(minutes=240, per_minute=9.0, seed=7, now=NOW)
    res = scan(pays)
    assert res.action in (Action.ALTERNATE_METHOD_LINK, Action.SWITCH_PSP)


def test_a_thin_book_reports_that_it_is_thin_rather_than_guessing():
    """The live account's own state, and the reason this whole module exists.
    Under-powered is a result to report, not a reason to lower the threshold."""
    pays, _ = T.generate(minutes=30, per_minute=0.4, seed=5, now=NOW,
                         incident=False)
    res = scan(pays)
    assert res.thin
    assert not res.incident


# ---------------------------------------------------------------------------
# subscription failure
# ---------------------------------------------------------------------------

def test_the_live_ingest_says_precisely_why_it_cannot_run(monkeypatch):
    """Typed, so a caller can tell "this merchant lacks the feature" -- a fact
    to report -- from "the call broke", which is a fault to retry.

    `allow_local=False` because `collect()` now falls back to the local book
    rather than raising. The reason still has to be exact and reachable: it is
    what tells someone the fix is a dashboard toggle, not a bug.
    """
    from src.ingest import subscription_source as S
    def unavailable(*args, **kwargs):
        raise RuntimeError("razorpay 401: subscription feature unavailable")
    monkeypatch.setattr(S, "_call", unavailable)
    with pytest.raises(S.SubscriptionsUnavailable) as e:
        S.collect(env={"test": "true"}, allow_local=False)
    assert "not enabled on this account" in str(e.value)


def test_the_live_api_is_tried_before_the_local_book_every_time(monkeypatch):
    """The fallback must never become a preference. The day Subscriptions is
    enabled the loop has to switch back with no code change, which only holds
    if the API is attempted on every call rather than remembered as broken."""
    from src.ingest import subscription_source as S
    calls = []
    def spy(*a, **k):
        calls.append(1)
        raise S.SubscriptionsUnavailable("test account lacks subscriptions")
    monkeypatch.setattr(S, "fetch_subscriptions", spy)
    from src.ingest.razorpay_source import RevenueAtRisk
    item = RevenueAtRisk("subscription_failure", "sub_local_fixture", NOW, 100000, "TEST", "card")
    monkeypatch.setattr(S, "read_local", lambda: [item])
    assert S.collect() == [item]
    assert S.collect() == [item]
    assert len(calls) == 2, "the API was not retried on the second call"


def test_the_local_book_carries_a_mandate_and_a_funds_date():
    """The two facts the loop turns on. `funds_return_at` decides whether a
    silent re-present captures; a dead instrument has neither."""
    from src.ingest import subscription_source as S
    rows = S.read_local()
    if not rows:
        pytest.skip("no local subscription book seeded")
    assert any(r.detail.get("mandate") for r in rows)
    assert all(r.kind == "subscription_failure" for r in rows)
    assert all(r.reference.startswith("sub_local_") for r in rows)


def test_a_subscription_is_routed_to_the_mandate_ladder():
    """`config/workflows.yaml` declared `schedule: mandate` and nothing read
    it. A failed recurring charge went onto the `slow` checkout ladder and
    every silent rung the workflow exists for was absent from the plan."""
    from src.recovery.declines import classify_subscription, load_declines
    cfg = load_declines()
    c = classify_subscription("insufficient_funds", cfg)
    assert c.schedule == "mandate"
    # The decline class is unchanged: insufficient funds is insufficient funds.
    # Only the conclusion differs, because a mandate is standing consent.
    assert c.decline_class == "SOFT_FUNDS"
    steps = cfg["schedules"]["mandate"]["steps"]
    assert sum(1 for s in steps if s.get("requires_mandate")) == 4


def test_the_silent_rungs_contact_nobody():
    """The entire value of a mandate. Four retries that spend no contact
    ceiling, interrupt nobody, and cost nothing to send."""
    from src.recovery.channels import get, load_workflows
    from src.recovery.declines import load_declines
    cfg, wf = load_declines(), load_workflows()
    contacting = set(cfg["compliance"]["contacting_channels"])
    silent = [s for s in cfg["schedules"]["mandate"]["steps"]
              if s.get("requires_mandate")]
    for s in silent:
        assert s["channel"] not in contacting
        assert get(s["channel"], wf).cost_paise == 0


def test_a_gateway_without_the_capability_refuses_rather_than_pretending():
    from src.gateways.razorpay_gateway import RazorpayGateway
    gw = RazorpayGateway(env={"RAZORPAY_KEY_ID": "rzp_test_x",
                              "RAZORPAY_KEY_SECRET": "s"})
    assert not gw.capabilities().can_charge_saved_instrument
    with pytest.raises(NotImplementedError):
        gw.charge_mandate(100000, "sub_x")
