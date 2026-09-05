"""Audit trail. SPEC §2, §11.

One record per decision: what was detected, what was diagnosed and at which
rung, what the policy chose, which bounds fired, and whether execution was
REAL or SIMULATED (§11 -- every action tagged, and rendered as such in the UI).

The audit log is the UI's only data source (§2: "4 screens, static read of
audit log"), so anything a screen needs to show has to be written here.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .policy import BoundDecision
from .schema import Action, Diagnosis


@dataclass(frozen=True)
class AuditRecord:
    scenario_id: str
    incident_id: str
    detected_at: datetime
    cells: list[str]
    dominant_source: str
    dominant_step: str | None

    level_used: str                     # L1 | L2 | L3 | ABSTAIN
    cause_node: str | None
    mechanism_family: str | None
    confidence: float
    l3_eligible: bool
    l3_abstain_reason: str | None

    action_chosen: str                  # what the policy decided
    action_executed: str                # what survived the bounds
    bounds_fired: list[str]
    escalated: bool

    execution_mode: str                 # "REAL" | "SIMULATED" (§11)
    amount_at_risk_paise: int
    evidence: dict = field(default_factory=dict)


def build(scenario_id: str, diagnosis: Diagnosis, decision: BoundDecision,
          detected_at: datetime, cells: list[str], dominant_source: str,
          dominant_step: str | None, amount_at_risk_paise: int,
          execution_mode: str = "SIMULATED", escalated: bool = False
          ) -> AuditRecord:
    return AuditRecord(
        scenario_id=scenario_id,
        incident_id=diagnosis.incident_id,
        detected_at=detected_at,
        cells=list(cells),
        dominant_source=dominant_source,
        dominant_step=dominant_step,
        level_used=diagnosis.level_used,
        cause_node=diagnosis.cause_node,
        mechanism_family=diagnosis.mechanism_family,
        confidence=diagnosis.confidence,
        l3_eligible=diagnosis.l3_eligible,
        l3_abstain_reason=diagnosis.l3_abstain_reason,
        action_chosen=decision.original.value,
        action_executed=decision.action.value,
        bounds_fired=list(decision.bounds_fired),
        escalated=escalated,
        execution_mode=execution_mode,
        amount_at_risk_paise=int(amount_at_risk_paise),
        evidence=dict(diagnosis.evidence),
    )


def to_json(rec: AuditRecord) -> dict:
    d = asdict(rec)
    d["detected_at"] = rec.detected_at.isoformat()
    return d


def write(path: Path, records: list[AuditRecord]) -> None:
    """JSON Lines: one record per line, appendable, and readable by the UI
    without loading the whole file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(to_json(r)) + "\n")


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def summarise(records: list[AuditRecord]) -> dict:
    """Counts the UI header and RESULTS.md both want."""
    by_level: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for r in records:
        by_level[r.level_used] = by_level.get(r.level_used, 0) + 1
        by_action[r.action_executed] = by_action.get(r.action_executed, 0) + 1
    return {
        "n_records": len(records),
        "by_level": by_level,
        "by_executed_action": by_action,
        "n_unknown": sum(1 for r in records if r.cause_node is None),
        "n_escalated": sum(1 for r in records if r.escalated),
        "n_bounded": sum(1 for r in records if r.bounds_fired),
        "n_real": sum(1 for r in records if r.execution_mode == "REAL"),
        "amount_at_risk_paise": sum(r.amount_at_risk_paise for r in records),
    }
