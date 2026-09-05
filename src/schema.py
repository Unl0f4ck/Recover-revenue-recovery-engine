"""Core schemas. SPEC §4, amended by v1.2.

Import rule: this module is shared by observation-side and evaluation-side
code, so it must stay free of any ground-truth *values*. `TrueEpisode` is a
type definition only; instances live in the episode ledger, which `src/eval/`
alone may read (SPEC §1.2, enforced by tests/test_leakage.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Action(str, Enum):
    SAME_RAIL_RETRY       = "SAME_RAIL_RETRY"
    SWITCH_PSP            = "SWITCH_PSP"
    ALTERNATE_METHOD_LINK = "ALTERNATE_METHOD_LINK"
    BACKOFF_REPRESENT     = "BACKOFF_REPRESENT"
    NO_ACTION             = "NO_ACTION"


@dataclass(frozen=True)
class TelemetryWindow:
    """PRIMARY analysis unit. 5-minute windows.

    Counts are sufficient statistics for the binomial GLM, the CUSUM and
    source-rate attribution (SPEC §4) — transaction-level events are never
    materialised across the warm-up period.
    """
    timestamp: datetime
    cell_id: str                    # partition-qualified, e.g. "issuer:ISSUER_A"
    method: str
    n_attempts: int
    failures_by_source: dict[str, int]
    # v1.3. §8.2 clause 5 derives mechanism_family from the source/step/reason
    # signature, but v1.1 gave this unit no step breakdown -- so on card cells
    # `issuer_degradation` and `card_auth_spike`, which share source
    # `issuer_bank` and differ only by step, were indistinguishable. Still
    # counts, so still consistent with "counts are sufficient statistics".
    failures_by_step: dict[str, int]
    amount_at_risk_paise: int


@dataclass(frozen=True)
class PaymentEvent:
    """Razorpay-shaped. Materialised only for demo, audit examples and real
    execution — not for the analysis path.

    NO root_cause field, by invariant (SPEC §1.2).
    """
    timestamp: datetime
    payment_id: str
    amount_paise: int
    method: str
    issuer: str | None
    psp: str
    network: str | None
    success: bool
    error_code: str | None
    error_source: str | None
    error_step: str | None
    error_reason: str | None


@dataclass(frozen=True)
class TrueEpisode:
    """Episode ledger. src/eval/ ONLY — never read by detector, attribution
    or policy code.
    """
    episode_id: str
    mechanism: str                  # incl. "none" and "ood_*"
    start: datetime
    end: datetime
    affected_nodes: list[str]
    affected_methods: list[str]
    severity: float

    # v1.2 (§5.2) — ground truth for the onset/propagation amendment.
    primary_cell: str | None        # ground-truth root; None when mechanism="none"
    onset_arm: str                  # "propagating" | "simultaneous"
    onset_offsets: dict[str, int]   # cell -> Δ in windows
    coupling_beta: float            # β_m; 0.0 on the simultaneous arm


@dataclass(frozen=True)
class Diagnosis:
    incident_id: str
    cause_node: str | None          # None => UNKNOWN
    mechanism_family: str | None
    level_used: str                 # L1 | L2 | L3 | ABSTAIN
    confidence: float
    l3_eligible: bool
    l3_abstain_reason: str | None
    evidence: dict = field(default_factory=dict)
