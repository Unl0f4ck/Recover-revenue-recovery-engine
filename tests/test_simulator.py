"""Day 1 acceptance. SPEC §5, §14.

Done when: a stream renders as TelemetryWindow counts; injected incidents are
visible; the ledger is separate from the observables.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.schema import TelemetryWindow, TrueEpisode
from src.simulator import failures as F
from src.simulator import mechanisms as M
from src.simulator import volume as V
from src.simulator.generate import (EVAL_WINDOWS, WARMUP_WINDOWS,
                                    build_cells, generate_scenario)

TOPOLOGY, MECHS = M.load_configs()


# ------------------------------------------------------------- §5.1 volume

def test_median_cell_hits_spec_target():
    """§5.1: median cell sees 40-60 attempts/window."""
    assert 40.0 <= V.median_cell_attempts() <= 60.0


def test_diurnal_varies_and_stays_positive():
    v = V.diurnal_factor(np.arange(V.WINDOWS_PER_DAY))
    assert v.min() > 0.0
    assert v.max() / v.min() > 1.3          # a real diurnal swing, not a flat line


# --------------------------------------------- §5.2 categorical outcomes

def test_exactly_one_outcome_per_attempt():
    """The §5.2 invariant: a single categorical draw, so successes plus all
    source-failures equal the attempt count exactly. No precedence rule can
    exist if this holds.
    """
    rng = np.random.default_rng(7)
    eta = {"gateway": -2.0, "issuer_bank": -1.5, "network": -3.0}
    for n in (0, 1, 40, 5000):
        ok, by_src = F.draw_counts(rng, n, eta)
        assert ok + sum(by_src.values()) == n


def test_probabilities_normalise_and_survive_severe_incidents():
    p_ok, p_src = F.outcome_probabilities({"gateway": 3.0, "issuer_bank": 2.5})
    assert p_ok > 0.0
    assert abs(p_ok + sum(p_src.values()) - 1.0) < 1e-12


def test_higher_eta_raises_that_source_share():
    _, base = F.outcome_probabilities({"gateway": -3.0, "issuer_bank": -3.0})
    _, hit = F.outcome_probabilities({"gateway": -3.0 + 2.0, "issuer_bank": -3.0})
    assert hit["gateway"] > base["gateway"]
    # and the untouched source's SHARE falls, because success mass is consumed
    assert hit["issuer_bank"] < base["issuer_bank"]


def test_smoothing_handles_zero_failures():
    """§6: (x + 0.5)/(n + 1) so a zero-failure window is not logit(0) = -inf."""
    assert np.isfinite(F.smoothed_logit(0, 50))
    assert np.isfinite(F.smoothed_logit(50, 50))


# ------------------------------------------- §5.2 v1.2 onset / propagation

def _episode(arm: str, offsets: dict[str, int], beta: float = 0.6) -> M.Episode:
    cells = [("ISSUER_A", "card"), ("ISSUER_B", "card")]
    return M.Episode(
        episode_id="t", mechanism="psp_degradation", start_w=100, end_w=130,
        ramp="step", severity=2.0, affected_cells=cells,
        affected_nodes=["psp:PSP_1"], source_by_method={"card": "gateway"},
        primary_cell=cells[0], onset_arm=arm,
        offsets=offsets, gammas={M.cell_key(c): 2.0 for c in cells}, beta=beta,
    )


def test_offset_delays_a_cells_onset():
    """The v1.2 fix. A cell with Delta = 3 contributes nothing for three
    windows after the mechanism starts; the primary cell contributes at once.
    """
    ep = _episode("propagating",
                  {"issuer:ISSUER_A|card": 0, "issuer:ISSUER_B|card": 3})
    a, b = ("ISSUER_A", "card"), ("ISSUER_B", "card")
    assert M.direct_term(ep, a, "gateway", 100) > 0.0
    assert M.direct_term(ep, b, "gateway", 100) == 0.0
    assert M.direct_term(ep, b, "gateway", 102) == 0.0
    assert M.direct_term(ep, b, "gateway", 103) > 0.0


def test_simultaneous_arm_has_no_lead_lag_and_no_cascade():
    """The 40% control arm must stay exactly as v1.1 behaved, so an L3 null on
    this subset remains a real finding (§12.5).
    """
    ep = _episode("simultaneous",
                  {"issuer:ISSUER_A|card": 0, "issuer:ISSUER_B|card": 0}, beta=0.0)
    a, b = ("ISSUER_A", "card"), ("ISSUER_B", "card")
    assert M.direct_term(ep, a, "gateway", 100) == M.direct_term(ep, b, "gateway", 100)
    nb = M.build_neighbours(TOPOLOGY, build_cells(TOPOLOGY))
    dev = {("issuer:ISSUER_B|card", "gateway"): 2.0}
    assert M.cascade_term(ep, a, "gateway", 100, dev, nb) == 0.0


def test_cascade_is_zero_at_baseline_and_positive_on_deviation():
    """The Day 1 correction: coupling carries the DEVIATION, so calm periods
    propagate nothing and no constant offset leaks into the seasonal fit.
    """
    ep = _episode("propagating",
                  {"issuer:ISSUER_A|card": 0, "issuer:ISSUER_B|card": 0})
    nb = M.build_neighbours(TOPOLOGY, build_cells(TOPOLOGY))
    a = ("ISSUER_A", "card")
    assert M.cascade_term(ep, a, "gateway", 100, {}, nb) == 0.0
    hot = {("issuer:ISSUER_B|card", "gateway"): 1.5}
    assert M.cascade_term(ep, a, "gateway", 100, hot, nb) > 0.0


def test_severity_jitter_makes_cells_heterogeneous():
    """gamma_{m,p} = gamma_m * U(0.6, 1.4): the shift is no longer a constant
    common factor across the affected set.
    """
    rng = np.random.default_rng(3)
    ep = M.sample_episode(rng, MECHS, TOPOLOGY, "psp_degradation", 2100, "x")
    vals = list(ep.gammas.values())
    assert len(set(np.round(vals, 6))) > 1
    assert min(vals) >= ep.severity * 0.6 - 1e-9
    assert max(vals) <= ep.severity * 1.4 + 1e-9


def test_onset_mixture_is_roughly_sixty_forty():
    rng = np.random.default_rng(11)
    arms = [M.sample_episode(rng, MECHS, TOPOLOGY, "issuer_degradation",
                             2100, f"x{i}").onset_arm for i in range(400)]
    frac = arms.count("propagating") / len(arms)
    assert 0.52 <= frac <= 0.68          # pre-registered 0.60


# ------------------------------------------------------ §3.2 mechanisms

@pytest.mark.parametrize("name", ["issuer_degradation", "psp_degradation",
                                  "card_auth_spike", "upi_network_degradation"])
def test_mechanism_scope_is_method_valid(name):
    """Every mechanism's dominant source must exist in the documented source
    vocabulary of every method it claims. This is what caught the netbanking
    gateway error on Day 0.
    """
    mech = MECHS["mechanisms"][name]
    for method in mech["method_scope"]:
        assert mech["dominant_source"] in TOPOLOGY["methods"][method]["sources"]


def test_ood_mechanisms_are_generator_only():
    """§3.2: OOD mechanisms never appear in mechanisms.yaml as recognizable
    classes. The attributor must never be able to enumerate them.
    """
    declared = set(MECHS["mechanisms"])
    for name in M.ood_specs(TOPOLOGY):
        assert name not in declared


def test_ood_partial_psp_timeout_matches_no_topology_node():
    """The sharpest abstention test: a gateway signature over an issuer set
    that no single PSP covers.
    """
    rng = np.random.default_rng(5)
    ep = M.sample_episode(rng, MECHS, TOPOLOGY, "ood_partial_psp_timeout",
                          2100, "x")
    psps = {TOPOLOGY["routing"][i]["psp"] for i, _ in ep.affected_cells}
    assert len(psps) >= 2


# ---------------------------------------------------- §5.3 streams, ledger

def test_stream_renders_as_telemetry_windows():
    """Day 1 acceptance, clause 1."""
    stream, _ = generate_scenario("t-1", "issuer_degradation", seed=42)
    got = list(stream.windows(cell_index=0))
    assert len(got) == stream.n_windows
    w = got[0]
    assert isinstance(w, TelemetryWindow)
    assert w.cell_id.startswith("issuer:")
    assert set(w.failures_by_source) == set(stream.sources[0])
    assert sum(w.failures_by_source.values()) <= w.n_attempts


def test_warmup_is_clean():
    """§5.3: 7 clean warm-up days. Seasonal parameters are fit on warm-up only
    and frozen before the incident window opens, so an incident inside the
    warm-up would contaminate the baseline it is measured against.
    """
    _, ledger = generate_scenario("t-2", "psp_degradation", seed=9)
    from src.simulator.generate import EPOCH
    for e in ledger:
        start_w = int((e.start - EPOCH).total_seconds() // (60 * V.MINUTES_PER_WINDOW))
        assert start_w >= WARMUP_WINDOWS


def test_ledger_is_separate_from_observables():
    """Day 1 acceptance, clause 3, and the §1.2 invariant. The Stream object
    must expose no ground-truth field at all.
    """
    stream, ledger = generate_scenario("t-3", "card_auth_spike", seed=13)
    assert isinstance(ledger[0], TrueEpisode)
    leaked = {"mechanism", "severity", "primary_cell", "onset_arm",
              "affected_nodes", "coupling_beta"} & set(vars(stream))
    assert not leaked, f"Stream exposes ground truth: {leaked}"


def test_null_scenario_injects_nothing_but_is_still_countable():
    """§12.1: the 48 nulls are deliberately not incidents, but they still need
    a ledger row so they are never folded into an incident denominator.
    """
    _, ledger = generate_scenario("t-4", "none", seed=21)
    assert len(ledger) == 1
    assert ledger[0].mechanism == "none"
    assert ledger[0].affected_nodes == []


def test_injected_incident_is_visible_in_the_affected_cells():
    """Day 1 acceptance, clause 2 -- quantified rather than eyeballed.

    The affected cells' dominant-source rate must rise clearly above their own
    warm-up baseline while unaffected cells stay put.
    """
    stream, ledger = generate_scenario("t-5", "psp_degradation", seed=101)
    from src.simulator.generate import EPOCH
    ep = ledger[0]
    start_w = int((ep.start - EPOCH).total_seconds() // 300)
    end_w = int((ep.end - EPOCH).total_seconds() // 300)
    affected = set(ep.onset_offsets)

    lifts = {True: [], False: []}
    for ci, cell in enumerate(stream.cells):
        if "gateway" not in stream.sources[ci]:
            continue
        j = stream.sources[ci].index("gateway")
        rate = stream.failures[ci][:, j] / np.maximum(stream.n_attempts[:, ci], 1)
        base = rate[:WARMUP_WINDOWS].mean()
        during = rate[start_w:end_w].mean()
        lifts[M.cell_key(cell) in affected].append(during / max(base, 1e-9))

    assert np.mean(lifts[True]) > 2.0 * np.mean(lifts[False])
