"""Day 4: hidden action-effect environment. SPEC §10, §10.1."""
from __future__ import annotations

import numpy as np
import pytest
from pathlib import Path

import yaml

from src.environment import (KNOWN_MECHANISMS, best_action, load_environment,
                             mechanism_key, optimal_action_flips,
                             sample_efficacies, value)

CFG = load_environment()
POLICY = yaml.safe_load(
    Path("config/policy.yaml").read_text(encoding="utf-8"))["actions"]
ACTIONS = ("SAME_RAIL_RETRY", "SWITCH_PSP", "ALTERNATE_METHOD_LINK",
           "BACKOFF_REPRESENT", "NO_ACTION")


def test_full_cross_product_is_present():
    """§10: keyed by true mechanism x executed action, not 'right action vs
    blind retry'. A missing cell would make attribution regret undefined for
    exactly the misdiagnosis cases the thesis is about.
    """
    for mech in (*KNOWN_MECHANISMS, "none", "ood"):
        for action in ACTIONS:
            assert action in CFG["recovery_probability"][mech]


def test_ood_variants_share_the_ood_row():
    assert mechanism_key("ood_partial_psp_timeout") == "ood"
    assert mechanism_key("issuer_degradation") == "issuer_degradation"
    assert mechanism_key("none") == "none"


def test_efficacies_are_reproducible_from_the_seed():
    """Every baseline must face the SAME world, or B2 and B3 are compared
    across different environments and the paired design of §12.4 is broken.
    """
    a, b = sample_efficacies(42), sample_efficacies(42)
    assert a.recovery == b.recovery and a.cost == b.cost
    assert sample_efficacies(43).recovery != a.recovery


def test_draws_stay_inside_the_frozen_ranges():
    for seed in range(20):
        eff = sample_efficacies(seed)
        for mech, row in CFG["recovery_probability"].items():
            for act, (lo, hi) in row.items():
                assert lo - 1e-9 <= eff.recovery[mech][act] <= hi + 1e-9


# --------------------------------------------------------------- §10.1 value

def test_no_action_costs_nothing_but_can_still_recover():
    eff = sample_efficacies(7)
    v = value("issuer_degradation", "NO_ACTION", 1_000_000, 9_000_000, eff)
    assert v.cost_paise == 0.0 and v.penalty_paise == 0.0
    assert v.recovered_paise > 0        # some payments retry themselves


def test_switch_psp_pays_against_the_whole_flow():
    """The one systemic action: rerouting a gateway hits customers who would
    have paid fine, so its cost base is `touched`, not `at_risk`.
    """
    eff = sample_efficacies(7)
    small = value("psp_degradation", "SWITCH_PSP", 1_000_000, 2_000_000, eff)
    large = value("psp_degradation", "SWITCH_PSP", 1_000_000, 20_000_000, eff)
    assert large.cost_paise > small.cost_paise
    assert large.recovered_paise == small.recovered_paise   # at_risk unchanged


def test_customer_contacting_actions_do_not_scale_with_untouched_flow():
    """A payment link goes only to customers whose payment failed, so a larger
    surrounding flow must not make it more expensive.
    """
    eff = sample_efficacies(7)
    a = value("issuer_degradation", "ALTERNATE_METHOD_LINK", 1_000_000, 2_000_000, eff)
    b = value("issuer_degradation", "ALTERNATE_METHOD_LINK", 1_000_000, 20_000_000, eff)
    assert a.cost_paise == pytest.approx(b.cost_paise)


def test_false_intervention_on_a_healthy_segment_is_negative():
    """§10's `none` row: nothing at risk, so recovery is undefined -- but
    intervening still costs. Without this, "an agent that always acts is
    dangerous" never enters the regret decomposition.
    """
    eff = sample_efficacies(7)
    assert value("none", "NO_ACTION", 0, 12_000_000, eff).net_paise == 0.0
    for act in ("SWITCH_PSP", "ALTERNATE_METHOD_LINK", "BACKOFF_REPRESENT"):
        assert value("none", act, 0, 12_000_000, eff).net_paise < 0


def test_needless_reroute_costs_more_than_a_needless_link():
    """The realistic ordering: pointlessly rerouting a healthy gateway should
    hurt far more than sending a few unnecessary payment links.
    """
    eff = sample_efficacies(7)
    reroute = value("none", "SWITCH_PSP", 0, 12_000_000, eff).net_paise
    link = value("none", "ALTERNATE_METHOD_LINK", 0, 12_000_000, eff).net_paise
    assert reroute < link < 0


def test_contact_penalty_fires_once_for_contacting_actions_only():
    eff = sample_efficacies(7)
    assert value("issuer_degradation", "SWITCH_PSP", 0, 0, eff).penalty_paise == 0.0
    assert (value("issuer_degradation", "ALTERNATE_METHOD_LINK", 0, 0, eff)
            .penalty_paise == eff.contact_penalty_paise)


# ------------------------------------------------- §10 falsifiability

def test_optimal_action_genuinely_flips():
    """§10: "ranges must overlap enough that the optimal action genuinely flips
    between draws". If one action always won, diagnosis quality would be
    unmeasurable -- there would be nothing to get wrong.
    """
    dist = optimal_action_flips(n=200)
    for mech, counts in dist.items():
        share = max(counts.values()) / sum(counts.values())
        assert len(counts) >= 3, f"{mech}: only {len(counts)} actions ever optimal"
        assert share < 0.85, f"{mech}: one action optimal {share:.0%} of draws"


def test_policy_action_is_the_modal_optimum_for_each_mechanism():
    """Not required by the spec, but if the §9 table were NOT usually right,
    B1 (oracle diagnosis) would underperform for policy reasons and the
    detection/attribution regret split would be muddied.
    """
    dist = optimal_action_flips(n=200)
    for mech, counts in dist.items():
        assert max(counts, key=counts.get) == POLICY[mech], (
            f"{mech}: policy says {POLICY[mech]}, modal optimum is "
            f"{max(counts, key=counts.get)}")


def test_acting_is_sometimes_worse_than_waiting():
    """§10: "ALTERNATE_METHOD_LINK's higher cost must sometimes make acting
    worse than waiting."
    """
    worse = 0
    for seed in range(200):
        eff = sample_efficacies(seed)
        act = value("card_auth_spike", "ALTERNATE_METHOD_LINK", 300_000, 4_000_000, eff)
        wait = value("card_auth_spike", "NO_ACTION", 300_000, 4_000_000, eff)
        worse += act.net_paise < wait.net_paise
    assert worse > 0, "acting was never worse than waiting"


def test_sensitivity_regimes_move_in_opposite_directions():
    """§10's sweep: pessimistic means LESS recovery and MORE cost, so a regime
    that scaled both the same way would not be a stress test at all.
    """
    p = sample_efficacies(5, regime="pessimistic")
    b = sample_efficacies(5, regime="base")
    o = sample_efficacies(5, regime="optimistic")
    m, a = "psp_degradation", "SWITCH_PSP"
    assert p.recovery[m][a] < b.recovery[m][a] < o.recovery[m][a]
    assert p.cost[a] > b.cost[a] > o.cost[a] or b.cost[a] == 0.0


def test_best_action_is_not_the_policy_table():
    """`best_action` knows the drawn efficacies; O* does not -- it has oracle
    DIAGNOSIS but still runs the fixed §9 table. The gap is policy regret.
    """
    disagreements = 0
    for seed in range(100):
        eff = sample_efficacies(seed)
        for mech in KNOWN_MECHANISMS:
            act, _ = best_action(mech, 1_500_000, 12_000_000, eff)
            disagreements += act != POLICY[mech]
    assert disagreements > 0, "policy table is never suboptimal; ranges too narrow"
