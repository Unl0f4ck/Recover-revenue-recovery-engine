"""Calibrate the CUSUM threshold h on clean streams. SPEC §7.1.

    "Calibrate h on warm-up to ~1 false alarm per cell per 48h (tunable, dev
     only); record the procedure in RESULTS.md."

PROCEDURE (this is the part that gets recorded, not just the number):

  1. Generate K null scenarios. Under §5.3 the warm-up is incident-free, and
     for kind="none" the evaluation period is incident-free too.
  2. Per cell, fit the overall seasonal model on the WARM-UP only (§6).
  3. Measure false alarms on that cell's EVALUATION period -- clean, and not
     seen by the fit. Calibrating on the same windows the baseline was fit to
     would be optimistically biased: residuals are small there by construction.
     Fit-then-monitor-forward is also what deployment actually does.
  4. Pool across all cells and scenarios, and take the smallest h on the grid
     whose pooled rate is at or below 1 alarm per cell per 48h.

This script lives outside src/ deliberately: it imports the generator, and
src/detector/ may not (§1.2, enforced by tests/test_leakage.py).

Usage:  python -m scripts.calibrate_detector [K]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from src.detector.cusum import (KAPPA, WINDOWS_PER_48H, calibrate_h,
                                expected_delay_windows)
from src.seasonal import day_of_week_supported, fit_binomial_glm
from src.simulator.generate import WARMUP_WINDOWS, generate_scenario

OUT = Path("data/dev/calibration.json")


def build_clean_streams(k_scenarios: int, seed0: int = 9000):
    """(x, n, p0) per cell, over the held-out clean evaluation period."""
    streams, dow_checks = [], []
    for i in range(k_scenarios):
        stream, _ = generate_scenario(f"cal-{i:03d}", "none", seed=seed0 + i)
        t_warm = np.arange(WARMUP_WINDOWS)
        t_eval = np.arange(WARMUP_WINDOWS, stream.n_windows)

        for ci in range(len(stream.cells)):
            n = stream.n_attempts[:, ci]
            total = stream.failures[ci].sum(axis=1)

            model = fit_binomial_glm(total[:WARMUP_WINDOWS], n[:WARMUP_WINDOWS], t_warm)
            p0 = model.predict(t_eval)
            streams.append((total[WARMUP_WINDOWS:], n[WARMUP_WINDOWS:], p0))

            if i == 0:
                dow_checks.append(day_of_week_supported(
                    total[:WARMUP_WINDOWS], n[:WARMUP_WINDOWS], t_warm))
    return streams, dow_checks


def delay_grid(h: float) -> list[dict]:
    """Analytic expected detection delay across the (severity x duration) grid
    of §3.2, for a median-volume cell. Tells us which corner is unreachable
    before we measure recall on it.
    """
    rows = []
    for sev in (0.8, 1.2, 1.6, 2.0, 2.5, 3.0):
        edd = expected_delay_windows(h, n_per_window=44.0, p0=0.075,
                                     severity_logodds=sev)
        rows.append({
            "severity": sev,
            "edd_windows": edd,
            "edd_minutes": edd * 5 if np.isfinite(edd) else None,
            "detectable_within_20min": bool(np.isfinite(edd) and edd <= 4),
            "detectable_within_180min": bool(np.isfinite(edd) and edd <= 36),
        })
    return rows


def main() -> None:
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    streams, dow = build_clean_streams(k)

    h, diag = calibrate_h(streams)
    n_cells = len(streams) // k

    diag["procedure"] = (
        "seasonal fit on warm-up (2016 windows), false alarms measured on the "
        "held-out clean evaluation period (576 windows), pooled across cells "
        "and scenarios; smallest h on the grid meeting the target"
    )
    diag["k_scenarios"] = k
    diag["cells_per_scenario"] = n_cells
    diag["cell_periods_of_48h"] = len(streams) * 576 / WINDOWS_PER_48H
    diag["day_of_week_check"] = dow[0] if dow else None
    # 1 alarm per cell per 48h  ->  0.5 per cell per day  ->  x n_cells
    diag["system_wide_alarms_per_day"] = 0.5 * diag["target_per_cell_per_48h"] * n_cells
    diag["expected_delay"] = delay_grid(h)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(diag, indent=2), encoding="utf-8")

    print(f"kappa                 {KAPPA}")
    print(f"chosen h              {h:.2f}")
    print(f"clean cell-48h periods{diag['cell_periods_of_48h']:9.0f}")
    print(f"per-cell FA rate      {[c for c in diag['curve'] if c[0] == h][0][2]:.3f} /48h")
    print(f"system-wide           {diag['system_wide_alarms_per_day']:.1f} alarms/day "
          f"across {n_cells} cells")
    d = diag["day_of_week_check"]
    print(f"day-of-week terms     supported={d['supported']}  "
          f"BIC {d['bic_base']:.0f} (base) vs {d['bic_full']:.0f} (with DOW)")
    print("\nanalytic expected detection delay, median cell:")
    for r in diag["expected_delay"]:
        m = r["edd_minutes"]
        print(f"  severity {r['severity']:.1f}  EDD {r['edd_windows']:6.1f} windows"
              f"  ({m:6.0f} min)" if m else f"  severity {r['severity']:.1f}  never",
              " <=20min" if r["detectable_within_20min"] else "")
    print(f"\nwritten to {OUT}")


if __name__ == "__main__":
    main()
