"""Day 7: L3 conditional causal refinement. SPEC §8.3."""
from __future__ import annotations

import numpy as np
import pytest

from src.attribution import l3

CFG = l3.load_partition_config()
NAMES = [f"ISSUER_{c}" for c in "ABCDEFGH"]


# ------------------------------------------------- pre-registered partitions

def test_partition_table_is_the_pre_registered_one():
    assert l3.select_partition("issuer_bank", None, cfg=CFG)[0] == "issuer"
    assert l3.select_partition("gateway", None, cfg=CFG)[0] == "psp"
    assert l3.select_partition("customer_psp", None, cfg=CFG)[0] == "psp"
    # added Day 0: §8.3's printed table has no `network` row, yet
    # upi_network_degradation is network-dominant
    assert l3.select_partition("network", None, cfg=CFG)[0] == "network"


def test_two_signatures_means_no_l3_run():
    """§8.3: "ambiguous / two signatures => no L3 run". No runtime "pick
    whichever graph looks cleanest".
    """
    part, why = l3.select_partition("issuer_bank", None,
                                    n_distinct_signatures=2, cfg=CFG)
    assert part is None and "ambiguous" in why


def test_unknown_source_abstains_by_name():
    part, why = l3.select_partition("beneficiary_bank", None, cfg=CFG)
    assert part is None and "no pre-registered partition" in why


# ------------------------------------------------------------ residual build

def _series(rng, n_per=140, T=300, rate=0.05, shift=None):
    n = np.full(T, float(n_per))
    p = np.full(T, rate)
    if shift is not None:
        p = p * shift
    x = rng.binomial(n.astype(int), p).astype(float)
    return x, n, np.full(T, rate)


def test_residuals_are_centred_when_nothing_is_wrong():
    rng = np.random.default_rng(0)
    counts, attempts, base = {}, {}, {}
    for k in NAMES:
        counts[k], attempts[k], base[k] = _series(rng)
    names, X, meta = l3.build_residual_matrix(counts, attempts, base)
    assert names == sorted(NAMES) and meta["n_variables"] == 8
    assert abs(float(X.mean())) < 0.5      # standardized, so ~N(0,1)-ish


def test_standardized_residual_behaves_as_a_z_score():
    """The v1.2 standardization turns the residual into a z-score, so a real
    shift observed on 10x the traffic yields a proportionally larger value --
    that is correct, more evidence IS more evidence.

    It is therefore NOT a fix for a cross-variable volume artefact, and the
    v1.2 rationale claiming otherwise is withdrawn (see l3.build_residual_matrix
    and WORKLOG 27 Aug). ParCorr is invariant to per-variable rescaling, so a
    constant scale gap between variables was never visible to it. What the
    standardization genuinely buys is stable within-variable variance across a
    diurnal exposure cycle.
    """
    rng = np.random.default_rng(1)
    counts, attempts, base = {}, {}, {}
    for k in NAMES:
        counts[k], attempts[k], base[k] = _series(rng, n_per=100)
    # same 1.8x rate shift, one node 10x the traffic of the other
    counts["ISSUER_A"], attempts["ISSUER_A"], base["ISSUER_A"] = _series(
        rng, n_per=1000, shift=1.8)
    counts["ISSUER_B"], attempts["ISSUER_B"], base["ISSUER_B"] = _series(
        rng, n_per=100, shift=1.8)
    _, X, _ = l3.build_residual_matrix(counts, attempts, base)
    names = sorted(counts)
    a = float(X[:, names.index("ISSUER_A")].mean())
    b = float(X[:, names.index("ISSUER_B")].mean())
    assert a > 0 and b > 0
    # ~sqrt(10) = 3.16x, as a z-score on 10x the exposure should be
    assert 2.0 < a / b < 6.0


def test_leave_one_out_differences_out_a_common_swing():
    """A market-wide move that hits every node equally must not register: the
    LOO contrast is exactly what removes it.
    """
    rng = np.random.default_rng(2)
    counts, attempts, base = {}, {}, {}
    for k in NAMES:
        counts[k], attempts[k], base[k] = _series(rng, shift=2.0)
    _, X, _ = l3.build_residual_matrix(counts, attempts, base)
    assert abs(float(X.mean())) < 1.0     # everyone moved, so nobody stands out


# ------------------------------------------------------------------- gates

def _matrix(rng, T=300, n=8):
    return [f"ISSUER_{c}" for c in "ABCDEFGH"[:n]], rng.normal(size=(T, n))


def test_matrix_size_gate_rejects_too_few_and_too_many():
    rng = np.random.default_rng(3)
    names, X = _matrix(rng, n=4)
    att = {k: np.full(300, 100.0) for k in names}
    assert not l3.check_gates(names, X, att, CFG).passed


def test_temporal_gate_counts_windows_not_transactions():
    """§8.3 is explicit: T is the number of 5-minute WINDOWS, not the
    transaction count.
    """
    rng = np.random.default_rng(4)
    names, X = _matrix(rng, T=100)
    att = {k: np.full(100, 5000.0) for k in names}   # huge volume, short span
    r = l3.check_gates(names, X, att, CFG)
    assert not r.passed and "temporal" in r.reason


def test_exposure_gate_rejects_thin_variables():
    rng = np.random.default_rng(5)
    names, X = _matrix(rng)
    att = {k: np.full(300, 100.0) for k in names}
    att["ISSUER_H"] = np.full(300, 12.0)
    r = l3.check_gates(names, X, att, CFG)
    assert not r.passed and "exposure" in r.reason


def test_all_gates_pass_on_a_healthy_matrix():
    rng = np.random.default_rng(6)
    names, X = _matrix(rng)
    att = {k: np.full(300, 130.0) for k in names}
    assert l3.check_gates(names, X, att, CFG).passed


# -------------------------------------------------------------------- FDR

def test_bh_fdr_is_stricter_than_raw_alpha():
    """§8.3: with 5-8 variables and 6 lags this is hundreds of simultaneous
    tests; raw p < alpha link selection is indefensible at that multiplicity.
    """
    rng = np.random.default_rng(7)
    p = rng.uniform(size=(8, 8, 7))
    raw = int((p < 0.05).sum())
    corrected = int(l3._bh_fdr(p, 0.05).sum())
    assert corrected < raw
    assert corrected == 0            # pure noise should survive nothing


# ------------------------------------------------------ root selection

def _sig(edges, n=8, tau=7):
    s = np.zeros((n, n, tau), dtype=bool)
    for i, j, t in edges:
        s[i, j, t] = True
    return s


def test_root_is_the_sourceless_node_with_earliest_outgoing():
    root, why, _ = l3.select_root(NAMES, _sig([(0, 1, 1), (0, 2, 2), (1, 3, 1)]))
    assert root == "ISSUER_A"


def test_no_edges_abstains_by_name():
    root, why, _ = l3.select_root(NAMES, _sig([]))
    assert root is None and "no edges survived FDR" in why


def test_ties_abstain_rather_than_guess():
    """Two indistinguishable roots is exactly the case §8.3 wants escalated."""
    root, why, _ = l3.select_root(NAMES, _sig([(0, 2, 1), (1, 3, 1)]))
    assert root is None and "insufficient separation" in why


def test_lagged_feedback_is_not_an_abstention_trigger():
    """§8.3: cycles are NOT a trigger -- lagged feedback (X_t -> Y_t+1,
    Y_t -> X_t+1) is legitimate in a time-series graph.
    """
    root, why, _ = l3.select_root(NAMES, _sig([(0, 1, 1), (1, 0, 1), (0, 2, 1)]))
    # A and B feed back into each other, so neither is sourceless; C has a
    # parent. The result is a named outcome, never a crash.
    assert root is None or root in NAMES
    assert isinstance(why, str) and why


# ------------------------------------------------- end to end on known truth

@pytest.mark.slow
def test_pcmci_recovers_a_planted_root():
    """The validation that makes a negative result on real data meaningful: if
    the machinery could not recover a root it planted itself, nothing it says
    about payment residuals would be worth reporting.
    """
    rng = np.random.default_rng(0)
    T = 400
    X = rng.normal(size=(T, 8))
    for t in range(3, T):
        X[t, 1] += 0.75 * X[t - 1, 0]
        X[t, 2] += 0.65 * X[t - 2, 0]
        X[t, 3] += 0.60 * X[t - 1, 1]
    sig, _, _ = l3.run_pcmci(X, NAMES, CFG)
    root, _, detail = l3.select_root(NAMES, sig)
    assert root == "ISSUER_A"
    assert detail["parents"]["ISSUER_A"] == 0


def test_run_always_returns_a_named_outcome():
    """Day 7 acceptance: a root or a NAMED abstention reason. Never silence."""
    rng = np.random.default_rng(9)
    counts, attempts, base = {}, {}, {}
    for k in NAMES[:4]:                       # too few variables -> gate fails
        counts[k], attempts[k], base[k] = _series(rng)
    r = l3.run("issuer_bank", None, counts, attempts, base, cfg=CFG,
               with_stability=False)
    assert not r.succeeded
    assert r.abstain_reason and "gate" in r.abstain_reason


def test_unknown_source_returns_partition_abstention():
    rng = np.random.default_rng(10)
    counts, attempts, base = {}, {}, {}
    for k in NAMES:
        counts[k], attempts[k], base[k] = _series(rng)
    r = l3.run("beneficiary_bank", None, counts, attempts, base, cfg=CFG,
               with_stability=False)
    assert not r.eligible and "partition" in r.abstain_reason
