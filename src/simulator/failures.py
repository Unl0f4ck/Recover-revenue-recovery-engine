"""Categorical failure generation. SPEC §5.2.

CRITICAL (§5.2): a single categorical outcome per attempt, never independent
per-source Bernoulli draws with a precedence tie-break. Precedence would let
high-precedence sources systematically suppress lower ones at high failure
rates, baking a diagnostic artifact into ground truth that the attributor would
then read as signal.

    P(source = s) = exp(eta_s) / (1 + sum_j exp(eta_j))
    P(success)    = 1          / (1 + sum_j exp(eta_j))

Success is the reference category, so eta is the log-odds of that source
against success. Counts are then one Multinomial draw per cell-window.
"""
from __future__ import annotations

import numpy as np

from .volume import WINDOWS_PER_DAY

# Baseline log-odds theta_{p,s} of each source against success, before
# seasonality. Calm-period totals land at roughly 6-9% failure depending on
# method, which is realistic for Indian retail rails.
# (tunable, dev only)
BASE_SOURCE_LOGODDS = {
    "issuer_bank":      -3.05,
    "gateway":          -3.95,
    "network":          -4.30,
    "customer_psp":     -4.05,
    "beneficiary_bank": -4.55,
}

# Per-issuer offset so cells are not identical at baseline; the attributor must
# work against real heterogeneity, not a flat null. (tunable, dev only)
ISSUER_OFFSET = {
    "ISSUER_A": -0.14, "ISSUER_B": 0.06, "ISSUER_C": -0.05, "ISSUER_D": 0.17,
    "ISSUER_E": 0.02,  "ISSUER_F": -0.11, "ISSUER_G": 0.21, "ISSUER_H": 0.09,
}

# Source-specific seasonality (a1, b1, a2, b2) on the log-odds scale. Distinct
# per source, so the per-source seasonal models of §6 are not redundant with
# the overall one. (tunable, dev only)
SOURCE_SEASONAL = {
    "issuer_bank":      (0.30, 0.14, 0.10, -0.05),
    "gateway":          (0.16, -0.09, 0.05, 0.03),
    "network":          (0.21, 0.05, 0.12, 0.02),
    "customer_psp":     (0.12, 0.08, 0.04, -0.03),
    "beneficiary_bank": (0.18, -0.04, 0.06, 0.01),
}


def seasonal_logodds(source: str, t: int | np.ndarray) -> np.ndarray:
    a1, b1, a2, b2 = SOURCE_SEASONAL[source]
    x = 2.0 * np.pi * (np.asarray(t) % WINDOWS_PER_DAY) / WINDOWS_PER_DAY
    return a1 * np.sin(x) + b1 * np.cos(x) + a2 * np.sin(2 * x) + b2 * np.cos(2 * x)


def baseline_eta(issuer: str, source: str, t: int) -> float:
    """theta_{p,s} + seasonal_s(t). The mechanism and cascade terms are added
    by the caller (src/simulator/mechanisms.py).
    """
    return (BASE_SOURCE_LOGODDS[source]
            + ISSUER_OFFSET[issuer]
            + float(seasonal_logodds(source, t)))


def outcome_probabilities(eta: dict[str, float]) -> tuple[float, dict[str, float]]:
    """Softmax with success as the reference category.

    Returns (p_success, {source: p_source}). Computed in a shifted-exponent
    form so a severe incident (eta up to ~0) cannot overflow.
    """
    if not eta:
        return 1.0, {}
    keys = list(eta)
    e = np.array([eta[k] for k in keys], dtype=float)
    m = max(0.0, float(e.max()))                 # include the implicit 0 for success
    ex = np.exp(e - m)
    denom = np.exp(-m) + ex.sum()
    p_src = ex / denom
    return float(np.exp(-m) / denom), {k: float(p) for k, p in zip(keys, p_src)}


def draw_counts(rng: np.random.Generator, n_attempts: int,
                eta: dict[str, float]) -> tuple[int, dict[str, int]]:
    """One Multinomial draw per cell-window. Exactly one observable outcome per
    attempt; no precedence rule anywhere (§5.2).

    Returns (n_success, {source: n_failures}).
    """
    p_success, p_src = outcome_probabilities(eta)
    if n_attempts <= 0:
        return 0, {s: 0 for s in p_src}
    keys = list(p_src)
    probs = np.array([p_success] + [p_src[k] for k in keys], dtype=float)
    probs = probs / probs.sum()                  # guard against float drift
    draw = rng.multinomial(n_attempts, probs)
    return int(draw[0]), {k: int(c) for k, c in zip(keys, draw[1:])}


def smoothed_logit(x: int, n: int) -> float:
    """q_hat = (x + 0.5) / (n + 1), then logit. SPEC §6 smoothing.

    Wherever a rate logit is formed manually, a window with zero source
    failures must not produce logit(0) = -inf.
    """
    q = (x + 0.5) / (n + 1.0)
    return float(np.log(q / (1.0 - q)))


# --------------------------------------------------- step breakdown (v1.3)
#
# §4 gave TelemetryWindow a failures_by_source breakdown and no step
# breakdown, but §8.2 clause 5 derives mechanism_family from the
# source/step/reason signature. On card cells `issuer_degradation` and
# `card_auth_spike` share source `issuer_bank` and are separated ONLY by step,
# so without this the two are indistinguishable in the primary analysis unit.
#
# Steps come from the documented per-method vocabularies (topology.yaml,
# verified against Razorpay docs Day 0). Only plausible (source, step) pairs
# are modelled, not the full cross product.
STEP_WEIGHTS: dict[str, dict[str, dict[str, float]]] = {
    "card": {
        "gateway": {"payment_initiation": 0.6, "payment_capture": 0.4},
        "issuer_bank": {"card_enrollment_check": 0.15,
                        "payment_authentication": 0.45,
                        "payment_authorization": 0.40},
    },
    "netbanking": {
        "issuer_bank": {"payment_authentication": 0.45,
                        "payment_authorization": 0.55},
    },
    "upi": {
        "customer_psp": {"payment_authentication": 0.6, "payment_response": 0.4},
        "gateway": {"payment_initiation": 0.45, "payment_request": 0.55},
        "network": {"payment_request_beneficiary_details": 0.5,
                    "payment_status_request": 0.5},
        "issuer_bank": {"payment_debit_request": 0.4,
                        "payment_debit_response": 0.6},
        "beneficiary_bank": {"payment_credit_response": 1.0},
    },
}

# Fraction of an incident's EXCESS failures that land on its signature step.
# Not 1.0 on purpose: a real authentication outage still produces some
# authorization-stage failures, and a generator that puts every excess failure
# on exactly the discriminating step would hand the attributor a cleaner signal
# than reality offers. (tunable, dev only)
STEP_CONCENTRATION = 0.75


def steps_for(method: str, source: str) -> list[str]:
    return list(STEP_WEIGHTS.get(method, {}).get(source, {}))


def split_into_steps(rng: np.random.Generator, method: str, source: str,
                     observed: int, expected: float,
                     signature_step: str | None) -> dict[str, int]:
    """Distribute one source's failures across its steps.

    Baseline failures follow the documented step mix. The EXCESS over the
    seasonal expectation -- i.e. the part the incident caused -- concentrates
    on the mechanism's signature step, which is what makes the step breakdown
    carry diagnostic information at all.
    """
    weights = STEP_WEIGHTS.get(method, {}).get(source, {})
    if not weights or observed <= 0:
        return {s: 0 for s in weights}

    names = list(weights)
    base_p = np.array([weights[s] for s in names], dtype=float)
    base_p /= base_p.sum()

    excess = 0
    if signature_step in weights:
        excess = int(round(max(0.0, observed - expected) * STEP_CONCENTRATION))
        excess = min(excess, observed)

    out = {s: 0 for s in names}
    if excess:
        out[signature_step] += excess
    remainder = observed - excess
    if remainder > 0:
        draw = rng.multinomial(remainder, base_p)
        for s, c in zip(names, draw):
            out[s] += int(c)
    return out
