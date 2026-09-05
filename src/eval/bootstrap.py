"""Paired statistics. SPEC §12.4.

All baselines run on the SAME frozen scenarios, so every comparison is paired.
That is not a detail: the between-scenario variance here is enormous (incidents
differ in severity, duration and amount at risk by more than an order of
magnitude) while the within-scenario difference between two arms is small. An
unpaired interval is dominated by variance the pairing cancels exactly.

  - paired bootstrap, B = 2000, percentile method, for value and accuracy deltas
  - McNemar (exact binomial) for attribution correctness
  - Wilson intervals for standalone proportions
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml
from scipy import stats

CONFIG = Path(__file__).resolve().parents[2] / "config"


def _defaults() -> dict:
    return yaml.safe_load((CONFIG / "eval.yaml").read_text(encoding="utf-8"))["bootstrap"]


@dataclass(frozen=True)
class Delta:
    point: float
    ci_lo: float
    ci_hi: float
    n: int

    @property
    def excludes_zero(self) -> bool:
        return self.ci_lo > 0.0 or self.ci_hi < 0.0

    def __str__(self) -> str:
        return f"{self.point:+,.0f} [{self.ci_lo:+,.0f}, {self.ci_hi:+,.0f}]"


def paired_bootstrap_delta(a, b, n_resamples: int | None = None,
                           seed: int | None = None) -> Delta:
    """Bootstrap the paired mean difference a - b.

    Resamples SCENARIO INDICES and carries both arms' values for each drawn
    index together. Resampling the two arms independently would break the
    pairing and inflate the interval -- the whole reason §12.4 insists the
    baselines share the same frozen scenarios.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape != b.shape:
        raise ValueError(f"paired arms must align: {a.shape} vs {b.shape}")
    d = _defaults()
    n_resamples = int(n_resamples or d["n_resamples"])
    rng = np.random.default_rng(d["seed"] if seed is None else seed)

    diff = a - b
    n = len(diff)
    if n == 0:
        return Delta(0.0, 0.0, 0.0, 0)

    idx = rng.integers(0, n, size=(n_resamples, n))
    means = diff[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return Delta(float(diff.mean()), float(lo), float(hi), n)


@dataclass(frozen=True)
class McNemar:
    b: int              # a correct, b wrong
    c: int              # a wrong, b correct
    p_value: float
    favours: str        # "a" | "b" | "neither"


def mcnemar_exact(a_correct, b_correct) -> McNemar:
    """Exact binomial McNemar on the discordant pairs. SPEC §12.4.

    Exact rather than chi-square: with ~160 known-mechanism scenarios the
    discordant count is routinely under 25, which is precisely where the
    chi-square approximation stops being trustworthy. Concordant pairs carry no
    information about which arm is better and are correctly ignored.
    """
    a = np.asarray(a_correct, dtype=bool)
    b = np.asarray(b_correct, dtype=bool)
    if a.shape != b.shape:
        raise ValueError(f"paired arms must align: {a.shape} vs {b.shape}")

    n_b = int(np.sum(a & ~b))
    n_c = int(np.sum(~a & b))
    if n_b + n_c == 0:
        # No disagreement at all. Not evidence of equality, just no evidence.
        return McNemar(0, 0, 1.0, "neither")

    p = float(stats.binomtest(n_b, n_b + n_c, 0.5).pvalue)
    favours = "a" if n_b > n_c else "b" if n_c > n_b else "neither"
    return McNemar(n_b, n_c, p, favours)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a standalone proportion.

    `src/attribution/evidence.py` has its own copy. Duplicated deliberately:
    src/eval/ must not import from src/attribution/, because the evaluation
    layer may read ground truth and the attribution layer may not (§1.2).
    Sharing six lines of arithmetic is not worth coupling those two.
    """
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z / d * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return float(max(0.0, centre - half)), float(min(1.0, centre + half))


@dataclass(frozen=True)
class RegretDecomposition:
    policy_regret: Delta | None
    detection_regret: Delta
    attribution_regret: Delta
    l1_l2_gap: Delta
    l3_value: Delta


def regret_decomposition(o_star, b1, b2, b3, oracle_optimal=None,
                         n_resamples: int | None = None,
                         seed: int | None = None) -> RegretDecomposition:
    """§12.3's decomposition, each term with a paired bootstrap CI.

        detection regret      = O* - B1
        attribution regret    = B1 - B3
        L1/L2 attribution gap = B1 - B2
        value added by L3     = B3 - B2

    `oracle_optimal` is not a §12.3 baseline. O* has oracle DIAGNOSIS but still
    runs the fixed §9 policy table, and §10 requires the optimal action to flip
    between efficacy draws -- so the table is sometimes wrong even given perfect
    diagnosis. Passing it in exposes POLICY regret, which the decomposition
    otherwise hides inside what looks like detection regret.
    """
    arms = {"o_star": o_star, "b1": b1, "b2": b2, "b3": b3}
    if oracle_optimal is not None:
        arms["oracle_optimal"] = oracle_optimal
    lengths = {k: len(np.asarray(v)) for k, v in arms.items()}
    if len(set(lengths.values())) != 1:
        raise ValueError(f"arms must be paired over the same scenarios: {lengths}")
    for k, v in arms.items():
        if not np.all(np.isfinite(np.asarray(v, dtype=float))):
            raise ValueError(f"arm {k!r} contains non-finite values")

    kw = {"n_resamples": n_resamples, "seed": seed}
    det = paired_bootstrap_delta(o_star, b1, **kw)
    attr = paired_bootstrap_delta(b1, b3, **kw)
    gap = paired_bootstrap_delta(b1, b2, **kw)
    l3 = paired_bootstrap_delta(b3, b2, **kw)

    # NOTE: (B1-B2) == (B1-B3) + (B3-B2) is an ALGEBRAIC IDENTITY -- it holds
    # for any four arrays by linearity of the mean, so asserting it detects
    # nothing. An earlier version of this function "guarded" it, which was
    # worse than no check: it looked like validation while being a tautology.
    # The real failure mode is arms that are not paired over the same scenarios,
    # or that carry non-finite values, so that is what is checked above.

    pol = (paired_bootstrap_delta(oracle_optimal, o_star, **kw)
           if oracle_optimal is not None else None)
    return RegretDecomposition(pol, det, attr, gap, l3)
