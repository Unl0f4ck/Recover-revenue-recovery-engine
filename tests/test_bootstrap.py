"""Day 5: paired statistics. SPEC §12.4."""
from __future__ import annotations

import numpy as np
import pytest

from src.eval.bootstrap import (mcnemar_exact, paired_bootstrap_delta,
                                regret_decomposition, wilson_interval)


def test_ci_contains_a_known_delta():
    rng = np.random.default_rng(0)
    base = rng.normal(0, 1, 400)
    # the DIFFERENCE must itself be noisy -- a constant offset gives a
    # degenerate zero-width interval, which is correct but tests nothing
    a = base + 2.0 + rng.normal(0, 0.5, 400)
    b = base
    d = paired_bootstrap_delta(a, b, seed=1)
    assert d.ci_lo < 2.0 < d.ci_hi
    assert d.excludes_zero


def test_zero_difference_gives_a_ci_containing_zero():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, 300)
    d = paired_bootstrap_delta(x, x.copy(), seed=2)
    assert d.ci_lo <= 0.0 <= d.ci_hi
    assert not d.excludes_zero


def test_pairing_is_what_makes_the_interval_usable():
    """The reason §12.4 insists every arm runs on the SAME scenarios.

    Both arms share a huge per-scenario offset and differ by a small constant --
    exactly the shape of this project's data, where incidents vary in value by
    orders of magnitude while the gap between two arms is comparatively tiny.
    Destroying the pairing by shuffling one arm leaves the difference swamped.
    """
    rng = np.random.default_rng(3)
    scenario_scale = rng.normal(0, 500_000, 300)   # incident size varies wildly
    a = scenario_scale + 5_000
    b = scenario_scale
    paired = paired_bootstrap_delta(a, b, seed=4)
    shuffled = paired_bootstrap_delta(a, rng.permutation(b), seed=4)

    paired_width = paired.ci_hi - paired.ci_lo
    shuffled_width = shuffled.ci_hi - shuffled.ci_lo
    assert paired_width < shuffled_width / 50
    assert paired.excludes_zero and not shuffled.excludes_zero


def test_seeding_is_reproducible():
    rng = np.random.default_rng(5)
    a, b = rng.normal(size=100), rng.normal(size=100)
    assert paired_bootstrap_delta(a, b, seed=9) == paired_bootstrap_delta(a, b, seed=9)
    assert (paired_bootstrap_delta(a, b, seed=9).ci_lo
            != paired_bootstrap_delta(a, b, seed=10).ci_lo)


def test_mismatched_arms_are_rejected():
    with pytest.raises(ValueError):
        paired_bootstrap_delta([1, 2, 3], [1, 2])


# ---------------------------------------------------------------- McNemar

def test_mcnemar_detects_asymmetric_discordance():
    a = [True] * 30 + [False] * 2
    b = [False] * 30 + [True] * 2
    r = mcnemar_exact(a, b)
    assert r.b == 30 and r.c == 2
    assert r.p_value < 0.001 and r.favours == "a"


def test_mcnemar_symmetric_discordance_is_not_significant():
    a = [True] * 10 + [False] * 10
    b = [False] * 10 + [True] * 10
    r = mcnemar_exact(a, b)
    assert r.p_value > 0.5 and r.favours == "neither"


def test_mcnemar_survives_tiny_and_zero_discordance():
    """Exactly why the exact test is used rather than chi-square: these counts
    are common at ~160 scenarios and the approximation is unreliable there.
    """
    tiny = mcnemar_exact([True, False, True], [False, False, True])
    assert 0.0 <= tiny.p_value <= 1.0
    none = mcnemar_exact([True, False], [True, False])
    assert none.b == 0 and none.c == 0 and none.p_value == 1.0
    assert none.favours == "neither"


def test_mcnemar_ignores_concordant_pairs():
    """Adding cases both arms get right cannot change the verdict."""
    a = [True] * 5 + [False] * 1
    b = [False] * 5 + [True] * 1
    small = mcnemar_exact(a, b)
    padded = mcnemar_exact(a + [True] * 500, b + [True] * 500)
    assert small.p_value == pytest.approx(padded.p_value)


# ----------------------------------------------------------------- Wilson

def test_wilson_brackets_and_widens_at_small_n():
    lo, hi = wilson_interval(50, 1000)
    assert lo < 0.05 < hi
    wide = wilson_interval(5, 100)
    assert (wide[1] - wide[0]) > (hi - lo)
    assert wilson_interval(0, 0) == (0.0, 1.0)


# ------------------------------------------------------------ decomposition

def test_regret_identity_holds_and_terms_are_paired():
    rng = np.random.default_rng(7)
    n = 200
    b2 = rng.normal(100_000, 40_000, n)
    b3 = b2 + rng.normal(3_000, 500, n)
    b1 = b3 + rng.normal(8_000, 900, n)
    o = b1 + rng.normal(20_000, 2_000, n)
    opt = o + rng.normal(5_000, 400, n)

    r = regret_decomposition(o, b1, b2, b3, oracle_optimal=opt, seed=11)
    assert r.detection_regret.excludes_zero
    assert r.l3_value.excludes_zero
    assert r.policy_regret is not None and r.policy_regret.excludes_zero
    # (B1-B2) == (B1-B3) + (B3-B2)
    assert r.l1_l2_gap.point == pytest.approx(
        r.attribution_regret.point + r.l3_value.point, abs=1e-6)


def test_decomposition_identity_is_algebraic_not_a_check():
    """(B1-B2) == (B1-B3) + (B3-B2) holds for ANY four arrays by linearity of
    the mean. Asserting it would validate nothing, so the function does not --
    it checks pairing and finiteness instead, which are the real failure modes.
    This test pins that understanding so nobody re-adds the tautology.
    """
    rng = np.random.default_rng(21)
    n = 60
    arbitrary = [rng.normal(0, 1e5, n) for _ in range(4)]
    r = regret_decomposition(*arbitrary, seed=1)
    assert r.l1_l2_gap.point == pytest.approx(
        r.attribution_regret.point + r.l3_value.point, abs=1e-6)


def test_unpaired_or_non_finite_arms_are_rejected():
    n = 40
    a = np.zeros(n)
    with pytest.raises(ValueError, match="same scenarios"):
        regret_decomposition(a, a, a, np.zeros(n + 1), seed=1)
    bad = a.copy(); bad[0] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        regret_decomposition(a, a, a, bad, seed=1)


def test_l3_value_is_zero_when_b3_equals_b2():
    """The current state of the build: L3 is not implemented, so B3 is B2 and
    the L3 term must come out at exactly zero with a CI containing zero.
    """
    rng = np.random.default_rng(13)
    b2 = rng.normal(50_000, 10_000, 120)
    r = regret_decomposition(b2 + 30_000, b2 + 20_000, b2, b2.copy(), seed=3)
    assert r.l3_value.point == pytest.approx(0.0)
    assert not r.l3_value.excludes_zero
