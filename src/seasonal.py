"""Seasonal baselines. SPEC §6.

TWO models per cell, fit on warm-up only (§5.3), binomial GLM weighted by n_t:

  1. overall failure baseline  P(any failure | cell, t)   -> the detector
  2. per-source baselines      P(source = s | cell, t)     -> L3

Basis: logit(p) = b0 + b1 sin(2pi t/288) + b2 cos(2pi t/288)
                     + b3 sin(4pi t/288) + b4 cos(4pi t/288)

Leakage (§1.2): this module takes plain count arrays. It never imports the
generator, the mechanism config or the ledger, and has no way to know whether
an incident is present.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

WINDOWS_PER_DAY = 288
N_BASIS = 5
RIDGE = 1e-6                    # numerical only, not regularization of interest


def design_matrix(t: np.ndarray) -> np.ndarray:
    """(T, 5) Fourier design. t is the absolute window index."""
    t = np.asarray(t, dtype=float)
    x = 2.0 * np.pi * (t % WINDOWS_PER_DAY) / WINDOWS_PER_DAY
    return np.column_stack([np.ones_like(x), np.sin(x), np.cos(x),
                            np.sin(2 * x), np.cos(2 * x)])


def dow_design(t: np.ndarray) -> np.ndarray:
    """Fourier basis plus 6 day-of-week dummies (Sunday as reference)."""
    base = design_matrix(t)
    day = (np.asarray(t) // WINDOWS_PER_DAY).astype(int) % 7
    dummies = np.zeros((len(base), 6))
    for k in range(1, 7):
        dummies[:, k - 1] = (day == k).astype(float)
    return np.column_stack([base, dummies])


@dataclass(frozen=True)
class SeasonalModel:
    coef: np.ndarray
    converged: bool
    n_obs: int

    def logit(self, t: np.ndarray | int) -> np.ndarray:
        X = design_matrix(np.atleast_1d(t))[:, :len(self.coef)]
        return X @ self.coef

    def predict(self, t: np.ndarray | int) -> np.ndarray:
        """Baseline probability p0(t)."""
        return 1.0 / (1.0 + np.exp(-self.logit(t)))


def fit_binomial_glm(x: np.ndarray, n: np.ndarray, t: np.ndarray,
                     design=design_matrix, max_iter: int = 50,
                     tol: float = 1e-9) -> SeasonalModel:
    """IRLS for a binomial GLM on grouped counts, weighted by n_t.

    n_t enters as the binomial denominator, so a window with 2,000 attempts
    carries a thousand times the weight of one with 2 -- which is the whole
    point of fitting on counts rather than on rates.
    """
    X = design(np.asarray(t))
    x = np.asarray(x, dtype=float)
    n = np.asarray(n, dtype=float)
    keep = n > 0
    X, x, n = X[keep], x[keep], n[keep]

    if len(x) == 0:
        return SeasonalModel(np.zeros(X.shape[1]), False, 0)

    # start from the smoothed pooled rate (§6 smoothing keeps this finite)
    p0 = (x.sum() + 0.5) / (n.sum() + 1.0)
    beta = np.zeros(X.shape[1])
    beta[0] = np.log(p0 / (1.0 - p0))

    converged = False
    for _ in range(max_iter):
        eta = X @ beta
        p = 1.0 / (1.0 + np.exp(-np.clip(eta, -30, 30)))
        w = n * p * (1.0 - p)
        w = np.maximum(w, 1e-10)
        z = eta + (x - n * p) / w
        XtW = X.T * w
        A = XtW @ X + RIDGE * np.eye(X.shape[1])
        try:
            new = np.linalg.solve(A, XtW @ z)
        except np.linalg.LinAlgError:
            break
        if np.max(np.abs(new - beta)) < tol:
            beta = new
            converged = True
            break
        beta = new

    return SeasonalModel(beta, converged, int(len(x)))


@dataclass(frozen=True)
class CellSeasonal:
    """Both models for one cell (§6)."""
    overall: SeasonalModel
    per_source: dict[str, SeasonalModel]


def fit_cell(n_attempts: np.ndarray, failures_by_source: dict[str, np.ndarray],
             t: np.ndarray) -> CellSeasonal:
    """Fit both models for one cell from warm-up counts.

    `failures_by_source` maps source -> (T,) failure counts. The overall model
    is fit on the row sums, which is the correct denominator for the detector:
    it monitors "any failure", not any particular source.
    """
    total = np.sum(list(failures_by_source.values()), axis=0)
    return CellSeasonal(
        overall=fit_binomial_glm(total, n_attempts, t),
        per_source={s: fit_binomial_glm(x, n_attempts, t)
                    for s, x in failures_by_source.items()},
    )


def deviance(x: np.ndarray, n: np.ndarray, p: np.ndarray) -> float:
    """Binomial deviance, for the day-of-week check below."""
    x, n, p = np.asarray(x, float), np.asarray(n, float), np.clip(p, 1e-12, 1 - 1e-12)
    a = np.where(x > 0, x * np.log(np.maximum(x, 1e-12) / (n * p)), 0.0)
    b = np.where(n - x > 0,
                 (n - x) * np.log(np.maximum(n - x, 1e-12) / (n * (1.0 - p))), 0.0)
    return float(2.0 * np.sum(a + b))


def day_of_week_supported(x: np.ndarray, n: np.ndarray, t: np.ndarray) -> dict:
    """SPEC §6: "Add day-of-week terms only if 7 days supports it -- check,
    don't assume."

    The check, not the assumption. With exactly 7 warm-up days each DOW level
    is identified by a single day, so its coefficient absorbs that one day's
    idiosyncratic noise; BIC should reject it. Returns the numbers either way
    so the decision is recorded rather than asserted.
    """
    base = fit_binomial_glm(x, n, t)
    full = fit_binomial_glm(x, n, t, design=dow_design)

    keep = np.asarray(n) > 0
    tk, xk, nk = np.asarray(t)[keep], np.asarray(x)[keep], np.asarray(n)[keep]
    p_base = 1.0 / (1.0 + np.exp(-(design_matrix(tk) @ base.coef)))
    p_full = 1.0 / (1.0 + np.exp(-(dow_design(tk) @ full.coef)))

    n_obs = int(keep.sum())
    bic_base = deviance(xk, nk, p_base) + N_BASIS * np.log(n_obs)
    bic_full = deviance(xk, nk, p_full) + (N_BASIS + 6) * np.log(n_obs)
    n_days = int(np.ptp(np.asarray(t) // WINDOWS_PER_DAY)) + 1

    return {
        "supported": bool(bic_full < bic_base) and n_days >= 14,
        "bic_base": bic_base,
        "bic_full": bic_full,
        "n_days": n_days,
        "note": ("fewer than 14 days: each DOW level rests on a single day, so "
                 "its coefficient is not separable from that day's noise"
                 if n_days < 14 else ""),
    }
