"""Recovery policy. SPEC §9.

Pure function Diagnosis -> Action, IDENTICAL across B1/B2/B3 (§1.2). Diagnosis
is the only varying input, so any difference in recovered value between the
baselines is attributable to diagnosis quality and nothing else.

`decide` is the pure mapping. `apply_bounds` is separate and stateful, because
bounds depend on what has already been done this incident -- keeping them apart
is what lets the policy identity invariant be checked by inspection.

Leakage (§1.2): reads policy.yaml only. Never the efficacy matrix, the
generator config or the ledger. This module does not know whether the diagnosis
it was handed is correct, and must not be able to find out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .yamlcache import load_yaml

from .schema import Action, Diagnosis

CONFIG = Path(__file__).resolve().parent.parent / "config"


def load_policy() -> dict:
    return load_yaml(CONFIG / "policy.yaml")


def decide(diagnosis: Diagnosis, cfg: dict | None = None) -> Action:
    """The pure mapping of §9's table. No state, no clock, no side effects."""
    cfg = cfg or load_policy()
    key = diagnosis.mechanism_family if diagnosis.cause_node else "UNKNOWN"
    if key is None:
        key = "UNKNOWN"
    return Action(cfg["actions"].get(key, "NO_ACTION"))


@dataclass
class BoundDecision:
    action: Action
    original: Action
    allowed: bool
    bounds_fired: list[str] = field(default_factory=list)
    escalate: bool = False


@dataclass(frozen=True)
class GateEvidence:
    """What the intervention gate is allowed to see. All of it is observable at
    decision time -- no ground truth, no efficacy matrix.
    """
    persistence_windows: int
    max_effect_logodds: float
    at_risk_paise: int
    touched_paise: int = 0


@dataclass
class GateDecision:
    authorized: bool
    reasons: list[str] = field(default_factory=list)
    expected_margin_paise: float = 0.0


def authorize(action: Action, ev: GateEvidence, cfg: dict | None = None
              ) -> GateDecision:
    """Should this diagnosis be ACTED on at all? SPEC §9 bounds, extended.

    The frozen run showed the system intervening on 44 of 48 null scenarios,
    while an oracle diagnosis on the SAME detector output intervened zero times.
    Diagnosis quality was not the problem: the pipeline acted on the first
    threshold crossing, and a random fluctuation crosses as readily as a real
    degradation.

    Four checks, in the order an operator would apply them:

      1. PERSISTENCE -- has this survived long enough to be real? A spurious
         alert reverts in ~3 windows; a genuine degradation runs ~15. No on-call
         system pages a human on a single 5-minute blip.
      2. EFFECT SIZE -- is the shift materially large, not merely detectable?
      3. EXPOSURE -- is there enough money at stake to be worth an intervention?
      4. EXPECTED VALUE -- does conservative expected recovery clear the action's
         cost by a margin? Uses an OPERATOR PRIOR on efficacy, not the hidden
         environment: what a payments team estimates from its own history is
         legitimately available to the policy layer.

    NO_ACTION always passes -- declining to act needs no authorisation.
    """
    cfg = cfg or load_policy()
    g = cfg.get("intervention_gate", {})
    if not g.get("enabled") or action is Action.NO_ACTION:
        return GateDecision(True, [])

    fails: list[str] = []
    if ev.persistence_windows < g["min_persistence_windows"]:
        fails.append(f"persistence {ev.persistence_windows}w < "
                     f"{g['min_persistence_windows']}w")
    if ev.max_effect_logodds < g["min_effect_logodds"]:
        fails.append(f"effect {ev.max_effect_logodds:.2f} < "
                     f"{g['min_effect_logodds']}")
    if ev.at_risk_paise < g["min_at_risk_paise"]:
        fails.append(f"exposure Rs {ev.at_risk_paise/100:,.0f} below floor")

    # conservative expected value, using the operator prior
    base = (ev.touched_paise if action is Action.SWITCH_PSP else ev.at_risk_paise)
    assumed_cost = {"SWITCH_PSP": 0.03, "ALTERNATE_METHOD_LINK": 0.15,
                    "BACKOFF_REPRESENT": 0.08, "SAME_RAIL_RETRY": 0.03}
    margin = (ev.at_risk_paise * float(g["assumed_recovery_probability"])
              - base * assumed_cost.get(action.value, 0.10))
    if margin < g["min_expected_margin_paise"]:
        fails.append(f"expected margin Rs {margin/100:,.0f} below "
                     f"Rs {g['min_expected_margin_paise']/100:,.0f}")

    return GateDecision(not fails, fails, margin)


@dataclass
class IncidentBudget:
    """Per-incident state the bounds need. Reset per incident."""
    actions_taken: int = 0
    last_action_at: datetime | None = None
    spend_paise: int = 0


def apply_bounds(action: Action, now: datetime, budget: IncidentBudget,
                 exposure_paise: int = 0, cfg: dict | None = None
                 ) -> BoundDecision:
    """§9 bounds, every firing logged.

    Returns the action actually permitted. A blocked action degrades to
    NO_ACTION and is recorded -- never silently dropped, because §12.5 reports
    the false-intervention rate and a silently suppressed action would flatter
    it.
    """
    cfg = cfg or load_policy()
    b = cfg["bounds"]
    fired: list[str] = []

    if action is Action.NO_ACTION:
        return BoundDecision(action, action, True, fired,
                             escalate=True)

    if b.get("global_kill_switch"):
        fired.append("global_kill_switch")

    if budget.actions_taken >= b["max_actions_per_incident"]:
        fired.append(f"max_actions_per_incident={b['max_actions_per_incident']}")

    if budget.last_action_at is not None:
        elapsed = (now - budget.last_action_at).total_seconds() / 60.0
        if elapsed < b["cooldown_minutes"]:
            fired.append(f"cooldown {elapsed:.0f}<{b['cooldown_minutes']}min")

    if action.value in cfg["customer_contacting"]:
        start, end = b["quiet_hours_ist"]
        hour = now.hour
        in_quiet = hour >= start or hour < end
        if in_quiet:
            fired.append(f"quiet_hours_ist {start:02d}:00-{end:02d}:00")

    if budget.spend_paise + exposure_paise > b["per_incident_spend_ceiling_paise"]:
        fired.append("per_incident_spend_ceiling")

    if fired:
        return BoundDecision(Action.NO_ACTION, action, False, fired, escalate=True)
    return BoundDecision(action, action, True, fired, escalate=False)


def should_escalate(diagnosis: Diagnosis, decision: BoundDecision) -> bool:
    """§9: UNKNOWN -> NO_ACTION + escalate. A bounded-out action escalates too:
    something was wrong enough to warrant acting and the system chose not to.
    """
    return diagnosis.cause_node is None or decision.escalate
