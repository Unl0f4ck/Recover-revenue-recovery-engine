"""Day 2 acceptance: does the detector fire on injected incidents, and what is
the measured delay? SPEC §14 Day 2.

Reports detection rate and delay stratified by severity and duration, because
that is where the answer actually lives -- a pooled recall number would hide
the corner the analytic EDD says is unreachable.

Usage:  python -m scripts.validate_detector [K_per_mechanism]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from src.detector.cusum import run_cusum
from src.incident import AlertContext, dominant_source, merge_alerts
from src.seasonal import fit_binomial_glm
from src.simulator.generate import EPOCH, WARMUP_WINDOWS, generate_scenario
from src.simulator.mechanisms import cell_key

MECHANISMS = ["issuer_degradation", "psp_degradation",
              "card_auth_spike", "upi_network_degradation"]


def _h() -> float:
    return json.loads(Path("data/dev/calibration.json").read_text())["chosen_h"]


def run_one(scenario_id: str, kind: str, seed: int, h: float):
    stream, ledger = generate_scenario(scenario_id, kind, seed=seed)
    t_warm = np.arange(WARMUP_WINDOWS)
    t_eval = np.arange(WARMUP_WINDOWS, stream.n_windows)

    contexts = []
    for ci, (issuer, method) in enumerate(stream.cells):
        n = stream.n_attempts[:, ci]
        total = stream.failures[ci].sum(axis=1)
        model = fit_binomial_glm(total[:WARMUP_WINDOWS], n[:WARMUP_WINDOWS], t_warm)
        p0 = model.predict(t_eval)
        for a in run_cusum(total[WARMUP_WINDOWS:], n[WARMUP_WINDOWS:], p0, h,
                           cell_index=ci):
            src = dominant_source(stream.failures[ci], stream.sources[ci],
                                  WARMUP_WINDOWS + a.start_window,
                                  WARMUP_WINDOWS + a.end_window)
            shifted = type(a)(a.cell_index, a.start_window + WARMUP_WINDOWS,
                              a.end_window + WARMUP_WINDOWS, a.peak)
            contexts.append(AlertContext(shifted, issuer, method, src))

    return stream, ledger, contexts, merge_alerts(contexts)


def main() -> None:
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    h = float(sys.argv[2]) if len(sys.argv) > 2 else _h()
    print(f"h = {h:.2f}\n")

    rows = []
    for kind in MECHANISMS:
        for i in range(k):
            stream, ledger, contexts, incidents = run_one(
                f"val-{kind[:4]}-{i:03d}", kind, seed=4000 + i, h=h)
            ep = ledger[0]
            st = int((ep.start - EPOCH).total_seconds() // 300)
            en = int((ep.end - EPOCH).total_seconds() // 300)
            affected = set(ep.onset_offsets)

            hits = [c for c in contexts
                    if cell_key((c.issuer, c.method)) in affected
                    and st - 2 <= c.alert.start_window <= en + 6]
            fps = [c for c in contexts
                   if cell_key((c.issuer, c.method)) not in affected]

            rows.append({
                "mechanism": kind, "severity": ep.severity,
                "duration_windows": en - st, "arm": ep.onset_arm,
                "detected": bool(hits),
                "delay": (min(c.alert.start_window for c in hits) - st) if hits else None,
                "cells_hit": len(hits), "cells_affected": len(affected),
                "false_cells": len(fps), "n_incidents": len(incidents),
            })

    Path("data/dev").mkdir(parents=True, exist_ok=True)
    Path("data/dev/detector_validation.json").write_text(json.dumps(rows, indent=2))

    def summarise(label, subset):
        if not subset:
            return
        det = [r for r in subset if r["detected"]]
        rate = len(det) / len(subset)
        dl = [r["delay"] for r in det if r["delay"] is not None]
        cov = np.mean([r["cells_hit"] / max(r["cells_affected"], 1) for r in det]) if det else 0
        print(f"  {label:<22} n={len(subset):3d}  detect={rate:5.1%}  "
              f"median delay={np.median(dl) if dl else float('nan'):5.1f}w  "
              f"cell coverage={cov:5.1%}")

    print("by mechanism")
    for m in MECHANISMS:
        summarise(m, [r for r in rows if r["mechanism"] == m])

    print("\nby severity")
    for lo, hi in [(0.8, 1.4), (1.4, 2.0), (2.0, 2.5), (2.5, 3.01)]:
        summarise(f"{lo:.1f}-{hi:.1f}", [r for r in rows if lo <= r["severity"] < hi])

    print("\nby duration")
    for lo, hi in [(4, 12), (12, 24), (24, 37)]:
        summarise(f"{lo*5}-{hi*5} min",
                  [r for r in rows if lo <= r["duration_windows"] < hi])

    print("\nby onset arm")
    for arm in ("propagating", "simultaneous"):
        summarise(arm, [r for r in rows if r["arm"] == arm])

    fp = np.mean([r["false_cells"] for r in rows])
    print(f"\nunaffected cells alerting per scenario (2 eval days): {fp:.2f}")
    print(f"incidents formed per scenario: {np.mean([r['n_incidents'] for r in rows]):.2f}")


if __name__ == "__main__":
    main()
