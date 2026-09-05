"""Sweep the CUSUM false-alarm target. SPEC §7.1 (tunable, dev only).

§7.1 sets the target at ~1 false alarm per cell per 48h. With 24 cells and a
2-day evaluation period that is ~24 spurious cell-alerts per scenario against
ONE true incident -- incident precision (§12.5) collapses to a few percent for
reasons that have nothing to do with attribution quality.

This sweep measures the detection/precision trade-off across targets so the
choice is made on evidence and recorded, per §0 rule 6 and the §13 discipline
note. Nothing here touches the frozen path: the dev pool only.

Usage:  python -m scripts.sweep_false_alarm_target
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from src.detector.cusum import calibrate_h, expected_delay_windows
from scripts.calibrate_detector import build_clean_streams
from scripts.validate_detector import MECHANISMS, run_one

TARGETS = [1.0, 0.3, 0.1, 0.03, 0.01]
N_CELLS = 24


def main() -> None:
    clean, _ = build_clean_streams(20)
    grid = np.concatenate([np.arange(2.0, 60.0, 0.5), np.arange(60.0, 400.0, 2.0)])

    rows = []
    for target in TARGETS:
        h, diag = calibrate_h(clean, target=target, grid=grid)
        achieved = [c for c in diag["curve"] if c[0] == h][0][2]

        det, delays, fp_cells = [], [], []
        for kind in MECHANISMS:
            for i in range(6):
                stream, ledger, contexts, incidents = run_one(
                    f"sw-{kind[:4]}-{i}", kind, seed=7000 + i, h=h)
                ep = ledger[0]
                from src.simulator.generate import EPOCH
                from src.simulator.mechanisms import cell_key
                st = int((ep.start - EPOCH).total_seconds() // 300)
                en = int((ep.end - EPOCH).total_seconds() // 300)
                aff = set(ep.onset_offsets)
                hits = [c for c in contexts
                        if cell_key((c.issuer, c.method)) in aff
                        and st - 2 <= c.alert.start_window <= en + 6]
                det.append(bool(hits))
                if hits:
                    delays.append(min(c.alert.start_window for c in hits) - st)
                fp_cells.append(sum(1 for c in contexts
                                    if cell_key((c.issuer, c.method)) not in aff))

        rows.append({
            "target_per_cell_per_48h": target,
            "h": h,
            "achieved_fa_rate": achieved,
            "detect_rate": float(np.mean(det)),
            "median_delay_windows": float(np.median(delays)) if delays else None,
            "fp_cells_per_scenario": float(np.mean(fp_cells)),
            "edd_sev_0.8": expected_delay_windows(h, 44.0, 0.075, 0.8),
            "edd_sev_1.6": expected_delay_windows(h, 44.0, 0.075, 1.6),
        })
        r = rows[-1]
        print(f"target={target:<5} h={h:6.1f}  detect={r['detect_rate']:5.1%}  "
              f"median delay={str(r['median_delay_windows']):>5}w  "
              f"FP cells/scenario={r['fp_cells_per_scenario']:5.1f}  "
              f"EDD@0.8={r['edd_sev_0.8']:5.1f}w")

    Path("data/dev").mkdir(parents=True, exist_ok=True)
    Path("data/dev/fa_target_sweep.json").write_text(json.dumps(rows, indent=2))
    print("\nwritten to data/dev/fa_target_sweep.json")


if __name__ == "__main__":
    main()
