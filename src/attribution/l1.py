"""L1 — deterministic attribution. SPEC §8.1.

  "Exactly one cell alerting in the partition, its residual log-odds shift
   exceeds threshold, all topological peers within their Wilson band. Return
   that cell, confidence = 0.9."

All three conditions, or L1 declines and the ladder falls through to L2.
"""
from __future__ import annotations

from dataclasses import dataclass

from .evidence import IncidentEvidence, load_eval


@dataclass(frozen=True)
class L1Result:
    matched: bool
    cause_node: str | None = None
    confidence: float = 0.0
    reason: str = ""


def run(ev: IncidentEvidence, cfg: dict | None = None) -> L1Result:
    conf = (cfg or load_eval())["attribution"]["l1"]

    alerting = ev.alerting
    if len(alerting) != 1:
        return L1Result(False, reason=f"{len(alerting)} cells alerting, need exactly 1")

    cell = alerting[0]
    if cell.shift < conf["min_shift_logodds"]:
        return L1Result(False, reason=(f"shift {cell.shift:.2f} below threshold "
                                       f"{conf['min_shift_logodds']}"))

    # All topological peers must sit inside their Wilson band -- i.e. none of
    # them shows a shift the interval can distinguish from zero. One peer
    # deviating means this is not a clean single-cell fault and L2 must weigh
    # the topology.
    deviating = [p.key for p in ev.peers if p.deviates]
    if deviating:
        return L1Result(False, reason=f"{len(deviating)} peers outside their band")

    return L1Result(True, cause_node=f"issuer:{cell.issuer}",
                    confidence=conf["confidence"],
                    reason="single deviating cell, all peers within band")
