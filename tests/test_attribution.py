"""Day 3: L1, L2, policy, audit. SPEC §8, §9."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.attribution import l1, l2
from src.attribution.evidence import (CellEvidence, IncidentEvidence,
                                      load_eval, load_topology,
                                      mechanism_family, residual_shift,
                                      two_proportion_z, wilson_interval)
from src.attribution.ladder import diagnose
from src.policy import (IncidentBudget, apply_bounds, decide, load_policy,
                        should_escalate)
from src.schema import Action, Diagnosis

EVAL = load_eval()
TOPOLOGY = load_topology()
IST = timezone(timedelta(hours=5, minutes=30))


def cell(issuer, method, x, n, p0, alerting):
    shift, lo, hi = residual_shift(x, n, p0)
    from src.attribution.evidence import one_proportion_z
    return CellEvidence(issuer, method, n, x, p0, shift, lo, hi,
                        one_proportion_z(x, n, p0), alerting)


def evidence(cells, source="gateway", methods=("card",), step=None):
    return IncidentEvidence("INC-000", source, step, list(methods),
                            cells, TOPOLOGY)


# ------------------------------------------------------------------ stats

def test_wilson_interval_brackets_the_estimate():
    lo, hi = wilson_interval(5, 100)
    assert lo < 0.05 < hi
    assert wilson_interval(0, 50)[0] == 0.0


def test_wilson_is_wider_at_small_n():
    narrow = wilson_interval(50, 1000)
    wide = wilson_interval(5, 100)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


def test_residual_shift_sign_and_zero():
    up, lo, _ = residual_shift(20, 100, 0.05)
    assert up > 0 and lo > 0
    flat, flo, fhi = residual_shift(5, 100, 0.05)
    assert abs(flat) < 0.3 and flo < 0 < fhi


# ------------------------------------------------------------------- §8.1 L1

def test_l1_fires_on_one_deviating_cell_with_quiet_peers():
    cells = [cell("ISSUER_A", "card", 60, 400, 0.05, True),
             cell("ISSUER_B", "card", 20, 400, 0.05, False),
             cell("ISSUER_C", "card", 18, 400, 0.05, False)]
    r = l1.run(evidence(cells), EVAL)
    assert r.matched and r.cause_node == "issuer:ISSUER_A"
    assert r.confidence == 0.9


def test_l1_declines_when_two_cells_alert():
    cells = [cell("ISSUER_A", "card", 60, 400, 0.05, True),
             cell("ISSUER_B", "card", 55, 400, 0.05, True)]
    assert not l1.run(evidence(cells), EVAL).matched


def test_l1_declines_when_a_peer_is_outside_its_band():
    """§8.1's third condition. A deviating peer means this is not a clean
    single-cell fault, and L2 must weigh the topology instead.
    """
    cells = [cell("ISSUER_A", "card", 60, 400, 0.05, True),
             cell("ISSUER_B", "card", 55, 400, 0.05, False)]
    r = l1.run(evidence(cells), EVAL)
    assert not r.matched and "peers" in r.reason


def test_l1_declines_on_a_shift_below_threshold():
    cells = [cell("ISSUER_A", "card", 23, 400, 0.05, True),
             cell("ISSUER_B", "card", 20, 400, 0.05, False)]
    assert not l1.run(evidence(cells), EVAL).matched


# ------------------------------------------------------------------- §8.2 L2

def _psp1_wide():
    """ISSUER_A and ISSUER_B are both PSP_1; C-H are elsewhere."""
    hot = [cell(i, "card", 80, 400, 0.05, True) for i in ("ISSUER_A", "ISSUER_B")]
    cold = [cell(i, "card", 20, 400, 0.05, False)
            for i in ("ISSUER_C", "ISSUER_D", "ISSUER_E", "ISSUER_F")]
    return hot + cold


def test_l2_prefers_the_topology_node_over_its_member_cells():
    """The Day 3 fix. Scoring cells against their OWN baseline and topology
    nodes against their peers puts them on different scales, and the parts then
    outrank the whole they belong to. Every candidate uses the same
    two-proportion test, so the PSP node wins.
    """
    r = l2.run(evidence(_psp1_wide()), EVAL)
    assert r.cause_node == "psp:PSP_1"
    assert r.mechanism_family == "psp_degradation"


def test_l2_scores_each_node_label_once():
    """An issuer appears once per alerting method and again as a topology node.
    Without dedup, one explanation occupies both the winner and runner-up slots
    and the margin test compares a hypothesis against itself.
    """
    r = l2.run(evidence(_psp1_wide()), EVAL)
    assert len({c.node for c in r.candidates}) == len(r.candidates)


def test_l2_returns_unknown_below_the_pre_registered_margin():
    """§8.2 clause 4: two explanations the data cannot separate get escalated,
    not guessed.
    """
    cells = [cell("ISSUER_A", "card", 80, 400, 0.05, True),
             cell("ISSUER_G", "card", 80, 400, 0.05, True),
             cell("ISSUER_C", "card", 20, 400, 0.05, False)]
    r = l2.run(evidence(cells), EVAL)
    assert r.is_unknown and "margin" in r.reason


def test_l2_returns_unknown_when_nothing_reaches_the_floor():
    cells = [cell(i, "card", 21, 400, 0.05, i == "ISSUER_A")
             for i in ("ISSUER_A", "ISSUER_B", "ISSUER_C")]
    assert l2.run(evidence(cells), EVAL).is_unknown


# ------------------------------------------------- §8.2 clause 5, family

def test_family_needs_the_source_step_pair_not_source_alone():
    """`issuer_degradation` and `card_auth_spike` share source `issuer_bank`."""
    assert mechanism_family("issuer_bank", "payment_authentication", ["card"]) \
        == "card_auth_spike"
    assert mechanism_family("issuer_bank", "payment_authorization", ["card"]) \
        == "issuer_degradation"


def test_family_falls_back_to_tightest_method_scope_without_a_step():
    """`dominant_step` is not observable from a TelemetryWindow (§4 gives no
    step breakdown), so scope is the fallback. A card-only incident is better
    explained by the cards-only family.
    """
    assert mechanism_family("issuer_bank", None, ["card"]) == "card_auth_spike"
    assert mechanism_family("issuer_bank", None, ["card", "upi"]) == "issuer_degradation"


def test_family_is_unambiguous_where_source_is_unique():
    assert mechanism_family("gateway", None, ["card"]) == "psp_degradation"
    assert mechanism_family("network", None, ["upi"]) == "upi_network_degradation"
    assert mechanism_family("beneficiary_bank", None, ["upi"]) is None   # OOD


# -------------------------------------------------------------- §8 ladder

def test_ladder_returns_l1_when_l1_matches():
    cells = [cell("ISSUER_A", "card", 60, 400, 0.05, True),
             cell("ISSUER_B", "card", 20, 400, 0.05, False)]
    dx = diagnose(evidence(cells), EVAL)
    assert dx.level_used == "L1" and dx.confidence == 0.9


def test_ladder_falls_through_to_l2():
    dx = diagnose(evidence(_psp1_wide()), EVAL)
    assert dx.level_used == "L2" and dx.cause_node == "psp:PSP_1"
    assert "l1_declined" in dx.evidence


# --------------------------------------------------------------- §9 policy

@pytest.mark.parametrize("family,expected", [
    ("issuer_degradation", Action.ALTERNATE_METHOD_LINK),
    ("psp_degradation", Action.SWITCH_PSP),
    ("card_auth_spike", Action.ALTERNATE_METHOD_LINK),
    ("upi_network_degradation", Action.BACKOFF_REPRESENT),
])
def test_policy_table(family, expected):
    dx = Diagnosis("i", "issuer:ISSUER_A", family, "L2", 0.6, False, None, {})
    assert decide(dx) is expected


def test_card_auth_spike_never_retries():
    """§9: retry re-triggers authentication."""
    dx = Diagnosis("i", "issuer:ISSUER_A", "card_auth_spike", "L2", 0.6, False, None, {})
    assert decide(dx) is not Action.SAME_RAIL_RETRY


def test_unknown_maps_to_no_action_and_escalates():
    dx = Diagnosis("i", None, None, "L2", 0.0, False, None, {})
    d = apply_bounds(decide(dx), datetime(2026, 9, 1, 14, 0, tzinfo=IST),
                     IncidentBudget())
    assert d.action is Action.NO_ACTION
    assert should_escalate(dx, d)


def test_policy_is_pure():
    """§1.2 policy identity: same diagnosis in, same action out, no state."""
    dx = Diagnosis("i", "psp:PSP_1", "psp_degradation", "L2", 0.6, False, None, {})
    assert {decide(dx) for _ in range(50)} == {Action.SWITCH_PSP}


# --------------------------------------------------------------- §9 bounds

def test_quiet_hours_block_customer_contact_only():
    night = datetime(2026, 9, 1, 23, 30, tzinfo=IST)
    blocked = apply_bounds(Action.ALTERNATE_METHOD_LINK, night, IncidentBudget())
    allowed = apply_bounds(Action.SWITCH_PSP, night, IncidentBudget())
    assert not blocked.allowed and "quiet_hours" in blocked.bounds_fired[0]
    assert allowed.allowed          # rail-side actions are not customer-visible


def test_cooldown_and_action_cap_fire():
    now = datetime(2026, 9, 1, 14, 0, tzinfo=IST)
    cooling = apply_bounds(Action.SWITCH_PSP, now,
                           IncidentBudget(actions_taken=1,
                                          last_action_at=now - timedelta(minutes=3)))
    assert not cooling.allowed
    capped = apply_bounds(Action.SWITCH_PSP, now, IncidentBudget(actions_taken=2))
    assert not capped.allowed


def test_spend_ceiling_fires():
    now = datetime(2026, 9, 1, 14, 0, tzinfo=IST)
    ceiling = load_policy()["bounds"]["per_incident_spend_ceiling_paise"]
    d = apply_bounds(Action.SWITCH_PSP, now, IncidentBudget(),
                     exposure_paise=ceiling + 1)
    assert not d.allowed


def test_blocked_action_degrades_to_no_action_and_is_recorded():
    """Never silently dropped: §12.5 reports the false-intervention rate, and a
    suppressed action that left no trace would flatter it.
    """
    night = datetime(2026, 9, 1, 23, 30, tzinfo=IST)
    d = apply_bounds(Action.ALTERNATE_METHOD_LINK, night, IncidentBudget())
    assert d.action is Action.NO_ACTION
    assert d.original is Action.ALTERNATE_METHOD_LINK
    assert d.bounds_fired and d.escalate
