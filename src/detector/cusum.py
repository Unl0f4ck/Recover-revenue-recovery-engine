"""Binomial likelihood CUSUM. SPEC §7.1.

Per cell, window t: n_t attempts, x_t failures, seasonal baseline p0(t),
alternative p1(t) = min(0.95, kappa * p0(t)).

    Lambda_t = x_t ln(p1/p0) + (n_t - x_t) ln((1-p1)/(1-p0))
    S_t      = max(0, S_{t-1} + Lambda_t)
    alert when S_t > h

n_t enters directly, so 50% of 2 attempts barely moves S_t while 50% of 2,000
moves it hard. That is the property a rate-based detector loses.

Leakage (§1.2): arrays and a fitted baseline in, alarms out. No import of the
generator, the mechanism config or the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

KAPPA = 1.6                     # (tunable, dev only) -- SPEC §7.1
P1_CAP = 0.95

# Ceiling on the accumulated statistic, as a multiple of h. (tunable, dev only)
#
# Found on Day 5: an unbounded S climbs far above h during a severe incident and
# then needs many windows of negative drift to fall back under it, so §7.2's
# "stays below h for 3 consecutive windows" was taking hours to satisfy. Alerts
# ran ~3x longer than the incidents that caused them -- detected spans of 103
# windows against a true 35 -- which the matcher's temporal IoU correctly
# scored at 0.19, below the 0.3 threshold, turning near-perfect detections into
# false positives AND false negatives at once.
#
# Truncating S is the standard bounded-CUSUM remedy. It costs nothing on
# detection speed (crossing h is what raises the alert, and the cap sits above
# h) and bounds the time to stand down. Day 2's validation could not have caught
# this: it asked "did an alert fire during the incident", not "does the alert
# SPAN match".
S_CAP_MULTIPLE = 1.5

# (tunable, dev only) -- SPEC §7.1 states ~1.0 per cell per 48h. Lowered to
# 0.1 on Day 2 evidence: at 1.0, 24 cells over a 2-day evaluation period
# produce ~19 spurious cell-alerts and ~20 merged incidents against ONE true
# incident, so incident precision (§12.5) collapses to a few percent for
# reasons unrelated to attribution. 0.1 is a WORKING value for Days 3-5, not a
# frozen one: the principled choice is the operating point that maximises
# simulated recovered value on the dev pool, which cannot be computed until
# environment.py and policy.py exist. §13 step 3 puts exactly this decision
# ("thresholds, kappa, h procedure") in the Day 6 pre-freeze pass.
# See WORKLOG 26 Aug (Day 2) and RESULTS.md.
TARGET_FALSE_ALARMS_PER_48H = 0.1
WINDOWS_PER_48H = 576


def log_likelihood_ratio(x: np.ndarray, n: np.ndarray, p0: np.ndarray,
                         kappa: float = KAPPA) -> np.ndarray:
    """Per-window Lambda_t."""
    p0 = np.clip(np.asarray(p0, dtype=float), 1e-9, 1 - 1e-9)
    p1 = np.clip(np.minimum(P1_CAP, kappa * p0), 1e-9, 1 - 1e-9)
    x = np.asarray(x, dtype=float)
    n = np.asarray(n, dtype=float)
    return x * np.log(p1 / p0) + (n - x) * np.log((1.0 - p1) / (1.0 - p0))


def cusum_path(lam: np.ndarray) -> np.ndarray:
    """S_t = max(0, S_{t-1} + Lambda_t). No reset -- the raw path."""
    s = 0.0
    out = np.empty(len(lam))
    for i, v in enumerate(lam):
        s = max(0.0, s + float(v))
        out[i] = s
    return out


@dataclass(frozen=True)
class Alert:
    cell_index: int
    start_window: int           # ESTIMATED CHANGEPOINT, not the crossing time
    end_window: int             # first window of the 3-window quiet run
    peak: float
    crossed_window: int = -1    # when S actually exceeded h, for delay reporting


def run_cusum(x: np.ndarray, n: np.ndarray, p0: np.ndarray, h: float,
              cell_index: int = 0, kappa: float = KAPPA,
              quiet_windows: int = 3) -> list[Alert]:
    """Stream one cell and emit alerts.

    The statistic resets to 0 after an alert closes, so a long incident yields
    one alert rather than a fresh alert every window. An alert closes when the
    statistic stays below h for `quiet_windows` consecutive windows (§7.2).
    """
    lam = log_likelihood_ratio(x, n, p0, kappa)
    alerts: list[Alert] = []
    s = 0.0
    firing = False
    start = 0
    peak = 0.0
    quiet = 0
    ceiling = h * S_CAP_MULTIPLE

    for t, v in enumerate(lam):
        s = min(max(0.0, s + float(v)), ceiling)
        if not firing:
            if s > h:
                firing, start, peak, quiet = True, t, s, 0
        else:
            peak = max(peak, s)
            if s <= h:
                quiet += 1
                if quiet >= quiet_windows:
                    alerts.append(Alert(cell_index, start, t - quiet + 1, peak))
                    firing, s, quiet = False, 0.0, 0
            else:
                quiet = 0

    if firing:
        alerts.append(Alert(cell_index, start, len(lam) - 1, peak))
    return alerts


# ------------------------------------------------------------ calibration

def false_alarm_rate(x: np.ndarray, n: np.ndarray, p0: np.ndarray, h: float,
                     kappa: float = KAPPA) -> float:
    """Alarms per 48h on a stream known to be clean."""
    alerts = run_cusum(x, n, p0, h, kappa=kappa)
    return len(alerts) / (len(x) / WINDOWS_PER_48H)


def calibrate_h(clean_streams: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
                target: float = TARGET_FALSE_ALARMS_PER_48H,
                kappa: float = KAPPA,
                grid: np.ndarray | None = None) -> tuple[float, dict]:
    """Choose the smallest h whose pooled false-alarm rate on CLEAN streams is
    at or below `target` alarms per cell per 48h (§7.1).

    `clean_streams` is a list of (x, n, p0) triples -- warm-up periods only,
    which §5.3 guarantees are incident-free. Pooling across many cells and
    scenarios matters: a single 7-day warm-up is only 3.5 windows of 48h, far
    too thin to estimate a 1-per-48h rate.

    Returns (h, diagnostics). The diagnostics are what §7.1 asks to be recorded
    in RESULTS.md -- the procedure, not just the number.
    """
    if grid is None:
        grid = np.concatenate([np.arange(2.0, 40.0, 0.5),
                               np.arange(40.0, 200.0, 2.0)])

    total_windows = sum(len(s[0]) for s in clean_streams)
    periods = total_windows / WINDOWS_PER_48H

    curve = []
    chosen = float(grid[-1])
    for h in grid:
        alarms = sum(len(run_cusum(x, n, p0, float(h), kappa=kappa))
                     for x, n, p0 in clean_streams)
        rate = alarms / periods
        curve.append((float(h), int(alarms), float(rate)))
        if rate <= target:
            chosen = float(h)
            break

    return chosen, {
        "kappa": kappa,
        "target_per_cell_per_48h": target,
        "n_clean_streams": len(clean_streams),
        "total_clean_windows": int(total_windows),
        "equivalent_48h_periods": float(periods),
        "grid_min": float(grid[0]),
        "grid_max": float(grid[-1]),
        "curve": curve,
        "chosen_h": chosen,
    }


# ------------------------------------------------- analytic detection delay

def expected_delay_windows(h: float, n_per_window: float, p0: float,
                           severity_logodds: float,
                           kappa: float = KAPPA) -> float:
    """Expected detection delay in windows, from the CUSUM drift rate.

    Under a shift of `severity_logodds` on the failure log-odds, the true rate
    becomes p*. The statistic then drifts by E[Lambda] per window and needs to
    cover h, so EDD ~ h / E[Lambda]. This is the standard CUSUM first-order
    result, and it tells us BEFORE running anything which corner of the
    (severity x duration) grid is unreachable -- rather than discovering it as
    a bad recall number on Day 5.

    Returns inf when the drift is non-positive (never detected).
    """
    p0 = float(np.clip(p0, 1e-9, 1 - 1e-9))
    p1 = float(np.clip(min(P1_CAP, kappa * p0), 1e-9, 1 - 1e-9))
    odds = p0 / (1.0 - p0) * np.exp(severity_logodds)
    p_star = odds / (1.0 + odds)

    per_attempt = (p_star * np.log(p1 / p0)
                   + (1.0 - p_star) * np.log((1.0 - p1) / (1.0 - p0)))
    drift = n_per_window * per_attempt
    return float(h / drift) if drift > 0 else float("inf")
