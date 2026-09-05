"""Score the baselines on the FROZEN evaluation set. SPEC §13 step 7.

Runs after the pre-registration tag and against `data/frozen/manifest.json`.
Nothing from §13 steps 3-5 may be altered on the basis of what comes out of
here -- that is the whole point of the tag.

Usage:  python -m scripts.run_frozen [regime]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from src.eval.bootstrap import mcnemar_exact, regret_decomposition, wilson_interval
from scripts.run_baselines import run_scenario

MANIFEST = Path("data/frozen/manifest.json")


def main() -> None:
    regime = sys.argv[1] if len(sys.argv) > 1 else "base"
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))

    rows = []
    for s in man["scenarios"]:
        rows.append(run_scenario(s["scenario_id"], s["kind"], s["seed"],
                                 regime=regime))

    out = Path(f"data/frozen/results_{regime}.json")
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    arms = {a: np.array([r["arms"][a]["net"] for r in rows]) / 100
            for a in ("O_STAR", "B0", "B1", "B2", "B3")}
    opt = np.array([r["oracle_optimal"] for r in rows]) / 100

    print(f"FROZEN SET   regime={regime}   n={len(rows)}\n")
    print(f"{'arm':<8} {'mean net Rs':>14} {'recovered':>13} {'cost':>13} "
          f"{'interv':>8} {'attr acc':>9}")
    for a in ("O_STAR", "B0", "B1", "B2", "B3"):
        rec = np.mean([r["arms"][a]["recovered"] for r in rows]) / 100
        cost = np.mean([r["arms"][a]["cost"] for r in rows]) / 100
        iv = np.mean([r["arms"][a]["interventions"] for r in rows])
        ok = sum(r["arms"][a]["attr_correct"] for r in rows)
        ap = sum(r["arms"][a]["attr_applicable"] for r in rows)
        acc = f"{ok/ap:.1%}" if ap else "n/a"
        print(f"{a:<8} {arms[a].mean():>14,.0f} {rec:>13,.0f} {cost:>13,.0f} "
              f"{iv:>8.2f} {acc:>9}")

    d = regret_decomposition(arms["O_STAR"], arms["B1"], arms["B2"], arms["B3"],
                             oracle_optimal=opt)
    print("\nregret decomposition, Rs per scenario, paired bootstrap 95% CI")
    print(f"  policy regret      (opt - O*)  {d.policy_regret}")
    print(f"  detection regret   (O*  - B1)  {d.detection_regret}")
    print(f"  attribution regret (B1  - B3)  {d.attribution_regret}")
    print(f"  L1/L2 gap          (B1  - B2)  {d.l1_l2_gap}")
    print(f"  value added by L3  (B3  - B2)  {d.l3_value}")

    tp = sum(r["tp"] for r in rows); fp = sum(r["fp"] for r in rows)
    fn = sum(r["fn"] for r in rows)
    rl, rh = wilson_interval(tp, tp + fn)
    pl, ph = wilson_interval(tp, tp + fp)
    print(f"\ndetection  TP={tp} FP={fp} FN={fn}")
    print(f"  recall    {tp/max(tp+fn,1):.1%}  [{rl:.1%}, {rh:.1%}]")
    print(f"  precision {tp/max(tp+fp,1):.1%}  [{pl:.1%}, {ph:.1%}]")

    nulls = [r for r in rows if r["kind"] == "none"]
    fi = sum(1 for r in nulls if r["arms"]["B2"]["interventions"] > 0)
    fl, fh = wilson_interval(fi, len(nulls))
    print(f"\nfalse-intervention rate on {len(nulls)} nulls (B2): "
          f"{fi/max(len(nulls),1):.1%}  [{fl:.1%}, {fh:.1%}]")

    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
