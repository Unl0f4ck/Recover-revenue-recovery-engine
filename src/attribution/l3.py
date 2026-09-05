"""L3 — conditional causal refinement. SPEC §8.3.

Runs EXACTLY as pre-registered in `config/partition_selector.yaml`, committed
before L3 existed. Nothing here was tuned against L3's behaviour.

Pipeline:
  partition selection (pre-registered table, keyed on the (source, step) pair)
    -> variables: per-issuer, aggregated over methods, dominant source only
    -> V_p(t): deseasonalized leave-one-out residual log-odds, variance-standardized
    -> gates (exposure, temporal, matrix size, partition unambiguous)
    -> PCMCI (ParCorr, tau_max=6, alpha=0.05) + BH-FDR on the final MCI edge set
    -> root selection: no significant parents inside the set, earliest outgoing
    -> block-bootstrap stability: same root must win in >= 70% of resamples

L3 IS THE ONLY RUNG THAT ABSTAINS (§8.3 ladder rule). An abstention never
erases a valid L2 diagnosis -- the system still acts on L2.

L3 may NOT posit an unobserved root (§8.2, §8.3). Its permitted claims are
temporal precedence among observed co-deviating cells, and residual structure
remaining after L2's topology attribution.

Leakage (§1.2): arrays and the public partition config in, a node label out.
Never the generator, the efficacy matrix or the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

CONFIG = Path(__file__).resolve().parents[2] / "config"


def load_partition_config() -> dict:
    return yaml.safe_load(
        (CONFIG / "partition_selector.yaml").read_text(encoding="utf-8"))


# ------------------------------------------------------- partition selection

def select_partition(dominant_source: str, dominant_step: str | None,
                     n_distinct_signatures: int = 1,
                     cfg: dict | None = None) -> tuple[str | None, str]:
    """Pre-registered table lookup. Returns (partition, reason).

    "ambiguous / two signatures => no L3 run" (§8.3). No runtime "pick whichever
    graph looks cleanest".
    """
    cfg = cfg or load_partition_config()
    if n_distinct_signatures > 1:
        return None, "ambiguous: two or more source signatures"
    for row in cfg["partitions"]:
        if row.get("ambiguous_or_two_signatures"):
            continue
        if row.get("dominant_source") == dominant_source:
            return row["partition"], f"{dominant_source} -> {row['partition']}"
        if row.get("method_local_auth") and dominant_step == "payment_authentication":
            return row["partition"], "method-local auth pattern -> method"
    return None, f"no pre-registered partition for source {dominant_source!r}"


# ------------------------------------------------------------ preprocessing

def _logit(p):
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    return np.log(p / (1 - p))


def build_residual_matrix(counts: dict[str, np.ndarray],
                          attempts: dict[str, np.ndarray],
                          baseline: dict[str, np.ndarray]
                          ) -> tuple[list[str], np.ndarray, dict]:
    """V_p(t), the L3 variable. SPEC §8.3 with the v1.2 standardization.

        Vtilde_p(t) = logit(q_p) - logit(q_-p) - [logit(p0_p) - logit(p0_-p)]
        V_p(t)      = Vtilde_p(t) / SE_p(t)

    LEAVE-ONE-OUT: q_-p pools every OTHER variable's counts, so the shared
    component (a market-wide swing, a seasonal miss) is differenced out. Keeping
    p inside its own baseline would mechanically couple the features.

    VARIANCE STANDARDIZATION (the v1.2 amendment) divides by the delta-method
    SE, which varies with EXPOSURE OVER TIME. Its real job is to stabilise
    within-variable heteroskedasticity: without it, a cell's residual is noisier
    at 03:00 than at 14:00 purely because fewer people are paying, and that
    diurnal variance pattern is shared across cells and reads as structure.

    CORRECTION (Day 7). The v1.2 rationale claimed this removes a CROSS-VARIABLE
    volume artefact -- that a busy cell would otherwise cross significance
    sooner and be nominated as root. That reasoning does not hold for a
    correlation-based CI test: ParCorr is invariant to per-variable rescaling,
    so a constant scale difference between variables was never visible to it in
    the first place (verified numerically). Volume still influences PCMCI, but
    through ATTENUATION -- a low-volume cell carries more binomial noise, which
    shrinks its measured correlations -- and standardisation does not fix that.
    The amendment is kept because the time-varying part is genuinely worth
    having; the claim about cross-variable artefacts is withdrawn. The
    pre-registered dev-pool diagnostic (root vs volume rank) is what actually
    tests for the artefact, and it is reported rather than assumed away.

    Counts are smoothed (x+0.5)/(n+1) per §6 so a zero-failure window is finite.
    """
    names = sorted(counts)
    T = len(next(iter(counts.values())))
    X = np.zeros((T, len(names)))

    tot_x = np.sum([counts[k] for k in names], axis=0).astype(float)
    tot_n = np.sum([attempts[k] for k in names], axis=0).astype(float)

    for j, k in enumerate(names):
        x = np.asarray(counts[k], dtype=float)
        n = np.maximum(np.asarray(attempts[k], dtype=float), 1.0)
        q = (x + 0.5) / (n + 1.0)

        x_o = np.maximum(tot_x - x, 0.0)
        n_o = np.maximum(tot_n - n, 1.0)
        q_o = (x_o + 0.5) / (n_o + 1.0)

        p0 = np.clip(np.asarray(baseline[k], dtype=float), 1e-9, 1 - 1e-9)
        # baseline-expected contrast: the other variables' pooled baseline,
        # exposure-weighted, so the seasonal term is differenced consistently
        p0_o = np.zeros(T)
        wsum = np.zeros(T)
        for k2 in names:
            if k2 == k:
                continue
            w = np.maximum(np.asarray(attempts[k2], dtype=float), 0.0)
            p0_o += w * np.clip(np.asarray(baseline[k2], dtype=float), 1e-9, 1 - 1e-9)
            wsum += w
        p0_o = np.divide(p0_o, np.maximum(wsum, 1.0))
        p0_o = np.clip(p0_o, 1e-9, 1 - 1e-9)

        raw = (_logit(q) - _logit(q_o)) - (_logit(p0) - _logit(p0_o))

        # delta-method SE of each logit, combined
        se = np.sqrt(1.0 / np.maximum(n * q * (1 - q), 1e-9)
                     + 1.0 / np.maximum(n_o * q_o * (1 - q_o), 1e-9))
        X[:, j] = raw / np.maximum(se, 1e-9)

    return names, X, {"T": T, "n_variables": len(names)}


# ------------------------------------------------------------------- gates

@dataclass(frozen=True)
class GateReport:
    passed: bool
    reason: str
    detail: dict = field(default_factory=dict)


def check_gates(names: list[str], X: np.ndarray, attempts: dict[str, np.ndarray],
                cfg: dict | None = None) -> GateReport:
    """§8.3's gates. All must pass, else abstain to L2."""
    g = (cfg or load_partition_config())["gates"]
    T, N = X.shape

    if not (g["min_variables"] <= N <= g["max_variables"]):
        return GateReport(False, f"matrix size {N} outside "
                                 f"[{g['min_variables']}, {g['max_variables']}]",
                          {"n_variables": N})
    if T < g["min_windows"]:
        # T is the number of 5-min WINDOWS, not transaction count (§8.3)
        return GateReport(False, f"temporal: {T} windows < {g['min_windows']}",
                          {"T": T})

    med = {k: float(np.median(v)) for k, v in attempts.items()}
    thin = [k for k, m in med.items() if m < g["min_attempts_per_cell_per_window"]]
    if thin:
        return GateReport(False, f"exposure: {len(thin)} variables below "
                                 f"{g['min_attempts_per_cell_per_window']} "
                                 f"attempts/window", {"thin": thin})

    if not np.all(np.isfinite(X)):
        return GateReport(False, "non-finite residuals", {})
    if np.any(X.std(axis=0) < 1e-8):
        return GateReport(False, "degenerate variable (zero variance)", {})

    return GateReport(True, "all gates passed", {"T": T, "n_variables": N})


# ------------------------------------------------------------------- PCMCI

def _bh_fdr(p: np.ndarray, alpha: float) -> np.ndarray:
    """Benjamini-Hochberg on the FINAL MCI edge set (§8.3).

    With 5-8 variables and 6 lags this is 200-400 simultaneous tests; raw
    p < alpha link selection is indefensible at that multiplicity.
    """
    flat = p.ravel()
    order = np.argsort(flat)
    m = len(flat)
    thresh = alpha * (np.arange(1, m + 1) / m)
    passed = flat[order] <= thresh
    k = np.max(np.nonzero(passed)[0]) + 1 if passed.any() else 0
    keep = np.zeros(m, dtype=bool)
    if k:
        keep[order[:k]] = True
    return keep.reshape(p.shape)


def run_pcmci(X: np.ndarray, names: list[str], cfg: dict | None = None):
    """PCMCI with ParCorr. Returns (significant_mask, p_matrix, val_matrix).

    `significant_mask[i, j, tau]` is edge  i --(lag tau)--> j  surviving BH-FDR.
    Lag 0 is excluded: contemporaneous links carry no temporal precedence, and
    precedence is the only thing L3 is permitted to claim.
    """
    c = (cfg or load_partition_config())["pcmci"]
    from tigramite import data_processing as pp
    from tigramite.independence_tests.parcorr import ParCorr
    from tigramite.pcmci import PCMCI

    df = pp.DataFrame(X, var_names=names)
    pcmci = PCMCI(dataframe=df, cond_ind_test=ParCorr(), verbosity=0)
    res = pcmci.run_pcmci(tau_max=int(c["tau_max"]), pc_alpha=float(c["alpha"]))

    p = np.asarray(res["p_matrix"])
    sig = _bh_fdr(p, float(c["alpha"]))
    sig[:, :, 0] = False                      # no contemporaneous edges
    for i in range(len(names)):
        sig[i, i, :] = False                  # self-lags are not precedence
    return sig, p, np.asarray(res["val_matrix"])


# --------------------------------------------------------- root selection

def select_root(names: list[str], sig: np.ndarray) -> tuple[str | None, str, dict]:
    """§8.3: among shortlisted observed nodes, the node with NO significant
    parents inside the set and the EARLIEST significant outgoing edges.

    Cycles are NOT an abstention trigger (§8.3): lagged feedback
    (X_t -> Y_t+1, Y_t -> X_t+1) is legitimate in a time-series graph.
    """
    n = len(names)
    parents = {names[j]: int(sig[:, j, :].sum()) for j in range(n)}
    outgoing = {names[i]: int(sig[i, :, :].sum()) for i in range(n)}
    if not any(outgoing.values()):
        return None, "no edges survived FDR", {"parents": parents}

    sourceless = [k for k, v in parents.items() if v == 0 and outgoing[k] > 0]
    if not sourceless:
        return None, "every node with outgoing edges also has parents", {
            "parents": parents, "outgoing": outgoing}

    def earliest(node: str) -> int:
        i = names.index(node)
        lags = np.nonzero(sig[i, :, :].any(axis=0))[0]
        return int(lags.min()) if lags.size else 99

    ranked = sorted(sourceless, key=lambda k: (earliest(k), -outgoing[k], k))
    if len(ranked) > 1:
        a, b = ranked[0], ranked[1]
        if earliest(a) == earliest(b) and outgoing[a] == outgoing[b]:
            return None, f"multiple roots with insufficient separation ({a}, {b})", {
                "parents": parents, "outgoing": outgoing}
    return ranked[0], "sourceless node with earliest outgoing edges", {
        "parents": parents, "outgoing": outgoing,
        "earliest_lag": earliest(ranked[0])}


def bootstrap_stability(X: np.ndarray, names: list[str], root: str,
                        cfg: dict | None = None, seed: int = 0) -> float:
    """Block bootstrap. The same root must win in >= 70% of resamples (§8.3).

    Blocks preserve the temporal dependence an iid resample would destroy --
    and temporal dependence is precisely what PCMCI reads.
    """
    c = (cfg or load_partition_config())["stability"]
    B = int(c["n_resamples"])
    T = X.shape[0]
    block = max(12, T // 20)
    rng = np.random.default_rng(seed)
    wins = 0
    for _ in range(B):
        starts = rng.integers(0, max(T - block, 1), size=(T // block) + 1)
        idx = np.concatenate([np.arange(s, min(s + block, T)) for s in starts])[:T]
        try:
            sig_b, _, _ = run_pcmci(X[idx], names, cfg)
            root_b, _, _ = select_root(names, sig_b)
        except Exception:
            root_b = None
        wins += int(root_b == root)
    return wins / B


# ------------------------------------------------------------------ driver

@dataclass(frozen=True)
class L3Result:
    eligible: bool
    root_node: str | None
    partition: str | None
    abstain_reason: str | None
    stability: float
    n_variables: int
    evidence: dict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.root_node is not None and self.abstain_reason is None


def run(dominant_source: str, dominant_step: str | None,
        counts: dict[str, np.ndarray], attempts: dict[str, np.ndarray],
        baseline: dict[str, np.ndarray], n_distinct_signatures: int = 1,
        cfg: dict | None = None, seed: int = 0,
        with_stability: bool = True) -> L3Result:
    """Full L3. Returns a root or a NAMED abstention reason -- never silence."""
    cfg = cfg or load_partition_config()

    partition, why = select_partition(dominant_source, dominant_step,
                                      n_distinct_signatures, cfg)
    if partition is None:
        return L3Result(False, None, None, f"partition: {why}", 0.0, 0)

    names, X, meta = build_residual_matrix(counts, attempts, baseline)
    gate = check_gates(names, X, attempts, cfg)
    if not gate.passed:
        return L3Result(False, None, partition, f"gate: {gate.reason}", 0.0,
                        meta["n_variables"], {"gate": gate.detail})

    sig, p, val = run_pcmci(X, names, cfg)
    root, why_root, detail = select_root(names, sig)
    if root is None:
        return L3Result(True, None, partition, f"root: {why_root}", 0.0,
                        len(names), {"edges": int(sig.sum()), **detail})

    stab = 1.0
    if with_stability:
        stab = bootstrap_stability(X, names, root, cfg, seed)
        floor = float(cfg["stability"]["min_root_agreement"])
        if stab < floor:
            return L3Result(True, None, partition,
                            f"root unstable: {stab:.0%} < {floor:.0%}",
                            stab, len(names), {"candidate_root": root, **detail})

    return L3Result(True, root, partition, None, stab, len(names),
                    {"edges": int(sig.sum()), "why": why_root, **detail})
