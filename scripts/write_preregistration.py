"""Generate PREREGISTRATION.md. SPEC §13 step 5.

Hashes every evaluation-critical constant BEFORE data/frozen/ exists, so the
tag provably predates the holdout. §13 lists what must be recorded: policy,
environment efficacy ranges, partition selector, L1 thresholds, L2 winner
margin, CUSUM kappa and h calibration procedure, incident merge thresholds,
matching threshold, L3 gates, tau_max, PCMCI significance procedure including
FDR, bootstrap block size and stability threshold, random seeds, metric
definitions.

Simulator constants are hashed too. They are evaluation-critical and live in
code rather than config, so hashing config alone would leave the generator free
to drift after the tag.

Usage:  python -m scripts.write_preregistration
"""
from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CONFIGS = [
    "config/environment.yaml", "config/policy.yaml", "config/eval.yaml",
    "config/partition_selector.yaml", "config/mechanisms.yaml",
    "config/topology.yaml", "config/signatures.yaml",
]
# Evaluation-critical code. Hashed because these carry frozen numeric constants
# that config does not (volume scale, baseline log-odds, step weights, the
# CUSUM statistic itself, the value formula).
CODE = [
    "src/simulator/volume.py", "src/simulator/failures.py",
    "src/simulator/mechanisms.py", "src/simulator/generate.py",
    "src/seasonal.py", "src/detector/cusum.py", "src/incident.py",
    "src/attribution/evidence.py", "src/attribution/l1.py",
    "src/attribution/l2.py", "src/attribution/ladder.py",
    "src/policy.py", "src/environment.py",
    "src/eval/matching.py", "src/eval/baselines.py", "src/eval/metrics.py",
    "src/eval/bootstrap.py",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                          text=True).stdout.strip()


def main() -> None:
    commit = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    frozen = REPO / "data" / "frozen"
    existing = sorted(p.name for p in frozen.glob("*")) if frozen.exists() else []

    lines = [
        "# PREREGISTRATION",
        "",
        "SPEC §13 step 5. Written **before** `data/frozen/` is generated, so the",
        "tag provably predates the holdout. Nothing hashed here may move",
        "afterwards; if any of it must, the correct remedy is a new tag and an",
        "explicit note, never a silent edit.",
        "",
        f"- Written: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Branch: `{branch}`",
        f"- Commit at time of writing: `{commit}`",
        f"- `data/frozen/` contents at time of writing: "
        f"**{'empty' if not existing else ', '.join(existing)}**",
        "",
        "---",
        "",
        "## Frozen configuration",
        "",
        "| file | sha256 |",
        "|---|---|",
    ]
    for rel in CONFIGS:
        p = REPO / rel
        lines.append(f"| `{rel}` | `{sha256(p) if p.exists() else 'MISSING'}` |")

    lines += [
        "",
        "## Frozen evaluation-critical code",
        "",
        "Hashed because these modules carry frozen numeric constants that the",
        "config files do not -- volume scale, baseline source log-odds, step",
        "weights, the CUSUM statistic and cap, and the value formula itself.",
        "Hashing config alone would leave the generator free to drift.",
        "",
        "| file | sha256 |",
        "|---|---|",
    ]
    for rel in CODE:
        p = REPO / rel
        lines.append(f"| `{rel}` | `{sha256(p) if p.exists() else 'MISSING'}` |")

    lines += [
        "",
        "---",
        "",
        "## Pre-registered decisions (§13's checklist)",
        "",
        "| item | value | where |",
        "|---|---|---|",
        "| CUSUM kappa | 1.6 | `eval.yaml` |",
        "| CUSUM h | **5.5** | `eval.yaml` |",
        "| h selection procedure | max B2 simulated recovered value on the dev "
        "pool, 64 scenarios evaluated paired across the h grid | "
        "`scripts/sweep_h_by_value.py`, `data/dev/h_sweep.json` |",
        "| CUSUM statistic cap | 1.5 x h (bounded CUSUM) | `detector/cusum.py` |",
        "| alert start | CUSUM changepoint estimate, not crossing time | "
        "`detector/cusum.py` |",
        "| incident merge | 15 min, routing-linked, same source family, "
        "connected components | `incident.py` |",
        "| incident close | 3 quiet windows | `eval.yaml` |",
        "| L1 min shift | 0.55 log-odds | `eval.yaml` |",
        "| L1 peer band | Wilson z = 1.96 | `eval.yaml` |",
        "| L1 confidence | 0.9 (fixed by §8.1) | `eval.yaml` |",
        "| L2 winner margin | z >= 1.5 over runner-up, else UNKNOWN | `eval.yaml` |",
        "| L2 min candidate | z >= 2.0 | `eval.yaml` |",
        "| L2 candidate scoring | one common pooled two-proportion test for "
        "cells and topology nodes alike | `attribution/l2.py` |",
        "| matching threshold | 0.3, temporal IoU x topology Jaccard | `eval.yaml` |",
        "| matcher | attribution-blind; mechanism family is a debug column only "
        "| `eval/matching.py` |",
        "| L3 partition selector | pre-registered table, keyed on (source, step) "
        "| `partition_selector.yaml` |",
        "| L3 variable set | per-issuer, aggregated over methods | "
        "`partition_selector.yaml` |",
        "| L3 gates | >=30 attempts/cell/window, >=288 windows, 5-8 variables | "
        "`partition_selector.yaml` |",
        "| PCMCI | ParCorr, tau_max = 6, alpha = 0.05, BH-FDR on the final MCI "
        "edge set | `partition_selector.yaml` |",
        "| L3 stability | block bootstrap B = 100, root must win >= 70% | "
        "`partition_selector.yaml` |",
        "| bootstrap | B = 2000, percentile, paired on scenarios | `eval.yaml` |",
        "| McNemar | exact binomial on discordant pairs | `eval/bootstrap.py` |",
        "| onset mixture | 60% propagating / 40% simultaneous | `mechanisms.yaml` |",
        "| onset offsets | Discrete{0,1,2,3,4} windows | `mechanisms.yaml` |",
        "| severity jitter | gamma x U(0.6, 1.4) per cell | `mechanisms.yaml` |",
        "| step concentration | 75% of excess on the signature step | "
        "`simulator/failures.py` |",
        "| efficacy ranges | full mechanism x action cross product | "
        "`environment.yaml` |",
        "| value formula | at_risk x p_recover - cost_base x cost - contact "
        "penalty | `environment.py` (§10.1) |",
        "| cost base | per-action; only SWITCH_PSP charges against touched flow "
        "| `environment.yaml` |",
        "| sensitivity regimes | pessimistic / base / optimistic | "
        "`environment.yaml` |",
        "| seeds | dev pool 9000, frozen 20260901, bootstrap 20260826 | "
        "`eval.yaml` |",
        "",
        "## Frozen set composition (§12.1)",
        "",
        "240 scenarios: 160 known-mechanism (40 each x 4), 48 null, 32 OOD.",
        "The 48 nulls are deliberately **not** incidents and are never folded",
        "into an incident denominator.",
        "",
        "## Metric definitions",
        "",
        "- **Attribution accuracy is always reported at coverage.** Accuracy",
        "  alone is not reportable: a system abstaining on 80% posts beautiful",
        "  conditional accuracy while being useless (§12.5).",
        "- **L3 metrics are split by onset arm** (propagating vs simultaneous)",
        "  and never pooled. A null on the simultaneous arm is expected and",
        "  correct; a null on the propagating arm is the real finding (§12.5).",
        "- **Policy regret** (`oracle_optimal - O*`) is reported alongside the",
        "  §12.3 decomposition. O* has oracle diagnosis but still runs the fixed",
        "  §9 table, and §10 requires the optimal action to flip between draws,",
        "  so the table is sometimes wrong even given a perfect diagnosis.",
        "  Without this term, policy regret inflates what looks like detection",
        "  regret.",
        "- All monetary figures are **simulated recovered value**, never",
        "  production revenue (§1.2).",
        "",
        "## Known caveat on the L3 rows",
        "",
        "L3 is implemented on Day 7, after this tag. Its gates and PCMCI",
        "settings above are copied from §8.3 and were not tuned -- there was",
        "nothing to tune them against. If L3 cannot run under them, changing",
        "them invalidates this tag and requires a second tag (`prereg-l3`)",
        "before B3 is scored on the frozen set. Recorded here rather than",
        "discovered later.",
    ]

    (REPO / "PREREGISTRATION.md").write_text("\n".join(lines) + "\n",
                                             encoding="utf-8")
    print(f"PREREGISTRATION.md written  ({len(CONFIGS)} configs, {len(CODE)} modules)")
    print(f"data/frozen/ is currently: {'empty' if not existing else existing}")


if __name__ == "__main__":
    main()
