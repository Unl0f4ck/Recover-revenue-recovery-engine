"""Arrival volume: Poisson with a diurnal factor. SPEC §5.1.

Numeric constants here are *(tunable, dev only)* per §0 rule 6 and must be
frozen before the holdout is generated. PREREGISTRATION.md hashes this module.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

WINDOWS_PER_DAY = 288          # 5-minute windows
MINUTES_PER_WINDOW = 5

# v(t) Fourier coefficients (a1, b1, a2, b2). Indian retail payments: a broad
# daytime peak with a secondary evening bump, quiet 02:00-06:00 IST.
DIURNAL = (0.38, -0.22, 0.14, 0.08)          # (tunable, dev only)

# Λ scale. Set so the MEDIAN cell sees 40-60 attempts/window (§5.1) -- above
# the L3 exposure gate of 30, but not so far above that power stops being a
# real constraint.
BASE_LAMBDA = 52.0                            # (tunable, dev only)

# Per-issuer volume share, normalized to median 1.0. Deliberately moderate
# dispersion: wide enough that volume confounding is real (§8.3's standardized
# residual has to earn its keep), narrow enough that low-volume cells do not
# sit permanently under the exposure gate.
ISSUER_SHARE = {                              # (tunable, dev only)
    "ISSUER_A": 1.55, "ISSUER_B": 1.20, "ISSUER_C": 1.05, "ISSUER_D": 1.00,
    "ISSUER_E": 0.95, "ISSUER_F": 0.85, "ISSUER_G": 0.75, "ISSUER_H": 0.60,
}

# Method mix. UPI dominates Indian retail; netbanking is the thin tail.
METHOD_SHARE = {"upi": 1.60, "card": 0.85, "netbanking": 0.45}   # (tunable)

# Amounts, lognormal in paise. median ~= Rs 900.
AMOUNT_LOG_MU = np.log(90_000.0)              # (tunable, dev only)
AMOUNT_LOG_SIGMA = 1.05                       # (tunable, dev only)


def diurnal_factor(t: np.ndarray | int) -> np.ndarray:
    """v(t) = 1 + a1 sin(2pi t/288) + b1 cos(...) + a2 sin(4pi t/288) + ...

    Clipped positive (§5.1). t is the absolute window index; the phase is
    t mod 288 so it repeats daily.
    """
    a1, b1, a2, b2 = DIURNAL
    x = 2.0 * np.pi * (np.asarray(t) % WINDOWS_PER_DAY) / WINDOWS_PER_DAY
    v = 1.0 + a1 * np.sin(x) + b1 * np.cos(x) + a2 * np.sin(2 * x) + b2 * np.cos(2 * x)
    return np.clip(v, 0.05, None)


@dataclass(frozen=True)
class CellVolume:
    issuer: str
    method: str

    @property
    def lam(self) -> float:
        return BASE_LAMBDA * ISSUER_SHARE[self.issuer] * METHOD_SHARE[self.method]


def draw_attempts(rng: np.random.Generator, lam: float, t: int) -> int:
    """n_t ~ Poisson(Lambda * v(t))."""
    return int(rng.poisson(lam * float(diurnal_factor(t))))


def draw_amounts(rng: np.random.Generator, n: int) -> np.ndarray:
    """Lognormal amounts in paise, rounded to whole paise."""
    if n <= 0:
        return np.zeros(0, dtype=np.int64)
    return np.rint(rng.lognormal(AMOUNT_LOG_MU, AMOUNT_LOG_SIGMA, n)).astype(np.int64)


def median_cell_attempts() -> float:
    """Day-averaged attempts/window for the median cell. Used by the Day 1
    acceptance check that §5.1's 40-60 target actually holds.
    """
    lams = sorted(CellVolume(i, m).lam
                  for i in ISSUER_SHARE for m in METHOD_SHARE)
    mid = lams[len(lams) // 2]
    return mid * float(np.mean(diurnal_factor(np.arange(WINDOWS_PER_DAY))))
