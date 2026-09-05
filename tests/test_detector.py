"""Day 2: seasonal baselines, binomial CUSUM, incident merge. SPEC §6, §7."""
from __future__ import annotations

import numpy as np
import pytest

from src.detector.cusum import (Alert, calibrate_h, cusum_path,
                                expected_delay_windows, log_likelihood_ratio,
                                run_cusum)
from src.incident import (AlertContext, can_merge, load_topology, merge_alerts)
from src.seasonal import (day_of_week_supported, design_matrix,
                          fit_binomial_glm, fit_cell)

TOPOLOGY = load_topology()
ROUTING = TOPOLOGY["routing"]


def _binomial_stream(rng, T=2016, n_per=50, coef=(-2.5, 0.4, 0.2, 0.1, -0.05)):
    t = np.arange(T)
    X = design_matrix(t)
    p = 1.0 / (1.0 + np.exp(-(X @ np.array(coef))))
    n = np.full(T, n_per)
    x = rng.binomial(n, p)
    return x, n, t, p


# ------------------------------------------------------------ §6 seasonal

def test_glm_recovers_known_diurnal_coefficients():
    rng = np.random.default_rng(0)
    true = (-2.5, 0.4, 0.2, 0.1, -0.05)
    x, n, t, _ = _binomial_stream(rng, coef=true)
    m = fit_binomial_glm(x, n, t)
    assert m.converged
    assert np.allclose(m.coef, true, atol=0.08)


def test_glm_is_weighted_by_attempts():
    """§6: binomial GLM weighted by n_t. A high-volume window must pull the
    fit far harder than a 2-attempt window claiming the same rate.
    """
    t = np.arange(576)
    n = np.full(576, 2)
    n[::2] = 4000                       # half the windows carry the evidence
    p_hi, p_lo = 0.05, 0.60
    x = np.where(np.arange(576) % 2 == 0,
                 np.rint(n * p_hi), np.rint(n * p_lo)).astype(int)
    m = fit_binomial_glm(x, n, t)
    fitted = float(m.predict(np.arange(576)).mean())
    assert abs(fitted - p_hi) < abs(fitted - p_lo)


def test_zero_failure_windows_do_not_break_the_fit():
    t = np.arange(288)
    n = np.full(288, 40)
    x = np.zeros(288, dtype=int)
    m = fit_binomial_glm(x, n, t)
    assert np.all(np.isfinite(m.coef))
    assert float(m.predict(t).max()) < 0.05


def test_day_of_week_is_not_supported_at_seven_days():
    """§6 says check, don't assume. With 7 warm-up days each DOW level rests on
    a single day, so its coefficient is not separable from that day's noise.
    """
    rng = np.random.default_rng(1)
    x, n, t, _ = _binomial_stream(rng, T=2016)
    chk = day_of_week_supported(x, n, t)
    assert chk["n_days"] == 7
    assert chk["supported"] is False


def test_fit_cell_produces_both_models():
    """§6: TWO models per cell -- overall for the detector, per-source for L3."""
    rng = np.random.default_rng(2)
    T = 1000
    n = np.full(T, 60)
    fbs = {"gateway": rng.binomial(n, 0.02), "issuer_bank": rng.binomial(n, 0.05)}
    cs = fit_cell(n, fbs, np.arange(T))
    assert set(cs.per_source) == {"gateway", "issuer_bank"}
    overall = float(cs.overall.predict(np.arange(T)).mean())
    assert 0.05 < overall < 0.10          # ~ the sum of the two source rates


# --------------------------------------------------------------- §7.1 CUSUM

def test_llr_sign_follows_the_observed_rate():
    p0 = np.array([0.05])
    assert log_likelihood_ratio(np.array([10.0]), np.array([100.0]), p0)[0] > 0
    assert log_likelihood_ratio(np.array([2.0]), np.array([100.0]), p0)[0] < 0


def test_attempt_count_enters_directly():
    """§7.1's whole point: 50% of 2 attempts barely moves S_t while 50% of
    2,000 moves it hard.
    """
    p0 = np.array([0.05])
    small = log_likelihood_ratio(np.array([1.0]), np.array([2.0]), p0)[0]
    large = log_likelihood_ratio(np.array([1000.0]), np.array([2000.0]), p0)[0]
    assert large > 100 * small


def test_cusum_path_is_non_negative_and_reflects_at_zero():
    lam = np.array([-5.0, -5.0, 3.0, 3.0])
    s = cusum_path(lam)
    assert (s >= 0).all()
    assert s[1] == 0.0
    assert s[3] == pytest.approx(6.0)


def test_alert_closes_after_three_quiet_windows():
    """§7.2: an incident ends when the CUSUM stays below h for 3 consecutive
    windows.
    """
    n = np.full(60, 100.0)
    p0 = np.full(60, 0.05)
    x = np.full(60, 5.0)
    x[10:20] = 40.0                     # a clear excursion, then back to baseline
    alerts = run_cusum(x, n, p0, h=5.0)
    assert len(alerts) == 1
    assert alerts[0].start_window >= 10


def test_calibration_is_monotone_in_h():
    rng = np.random.default_rng(4)
    streams = []
    for _ in range(6):
        x, n, t, p = _binomial_stream(rng, T=576, n_per=44)
        streams.append((x, n, p))
    lo, _ = calibrate_h(streams, target=5.0)
    hi, _ = calibrate_h(streams, target=0.2)
    assert hi >= lo


def test_expected_delay_falls_with_severity():
    d = [expected_delay_windows(5.0, 44.0, 0.075, s) for s in (0.8, 1.6, 3.0)]
    assert d[0] > d[1] > d[2] > 0


# ------------------------------------------------------- §7.2 incident merge

def _ctx(issuer, method, start, end, source, ci=0):
    return AlertContext(Alert(ci, start, end, 10.0), issuer, method, source)


def test_all_three_clauses_are_required():
    a = _ctx("ISSUER_A", "card", 100, 110, "gateway")       # PSP_1
    assert can_merge(a, _ctx("ISSUER_B", "card", 105, 115, "gateway"),
                     ROUTING, TOPOLOGY)                      # PSP_1, close, same family
    # clause 1 fails: 20 windows apart
    assert not can_merge(a, _ctx("ISSUER_B", "card", 140, 150, "gateway"),
                         ROUTING, TOPOLOGY)
    # clause 2 fails: ISSUER_G is PSP_4 / NET_2, no shared route on card
    assert not can_merge(a, _ctx("ISSUER_G", "card", 105, 115, "gateway"),
                         ROUTING, TOPOLOGY)
    # clause 3 fails: different source family
    assert not can_merge(a, _ctx("ISSUER_B", "card", 105, 115, "issuer_bank"),
                         ROUTING, TOPOLOGY)


def test_shared_network_links_only_upi_cells():
    """§3.1: networks are UPI-only, so a shared network cannot explain a link
    between two card cells.
    """
    # ISSUER_C (PSP_2/NET_1) and ISSUER_E (PSP_3/NET_1): different PSP, same net
    upi = can_merge(_ctx("ISSUER_C", "upi", 100, 110, "network"),
                    _ctx("ISSUER_E", "upi", 102, 112, "network"), ROUTING, TOPOLOGY)
    card = can_merge(_ctx("ISSUER_C", "card", 100, 110, "gateway"),
                     _ctx("ISSUER_E", "card", 102, 112, "gateway"), ROUTING, TOPOLOGY)
    assert upi and not card


def test_merge_is_transitive():
    """The documented choice: connected components, not cliques. A-B and B-C
    merge, A-C does not -- all three still land in one incident.
    """
    a = _ctx("ISSUER_A", "card", 100, 104, "gateway", ci=0)
    b = _ctx("ISSUER_B", "card", 106, 110, "gateway", ci=1)
    c = _ctx("ISSUER_B", "card", 112, 116, "gateway", ci=2)
    assert can_merge(a, b, ROUTING, TOPOLOGY)
    assert can_merge(b, c, ROUTING, TOPOLOGY)
    assert not can_merge(a, c, ROUTING, TOPOLOGY)      # 8 windows apart
    assert len(merge_alerts([a, b, c], TOPOLOGY)) == 1


def test_merge_is_order_independent():
    """"Deterministic" (§7.2) requires the result not to depend on the order
    alerts arrive in.
    """
    ctxs = [_ctx("ISSUER_A", "card", 100, 104, "gateway", 0),
            _ctx("ISSUER_B", "card", 106, 110, "gateway", 1),
            _ctx("ISSUER_G", "card", 300, 304, "issuer_bank", 2)]
    forward = merge_alerts(ctxs, TOPOLOGY)
    reverse = merge_alerts(list(reversed(ctxs)), TOPOLOGY)
    assert [i.cells for i in forward] == [i.cells for i in reverse]
    assert len(forward) == 2


def test_incident_reports_its_dominant_source_and_cells():
    inc = merge_alerts([_ctx("ISSUER_A", "card", 100, 104, "gateway", 0),
                        _ctx("ISSUER_B", "card", 102, 106, "gateway", 1)],
                       TOPOLOGY)[0]
    assert inc.dominant_source == "gateway"
    assert inc.cells == ["issuer:ISSUER_A|card", "issuer:ISSUER_B|card"]
    assert inc.methods == ["card"]
