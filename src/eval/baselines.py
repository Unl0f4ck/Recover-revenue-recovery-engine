"""The five baselines. SPEC §12.3.

|    | Detection    | Cause    | Policy |
|----|--------------|----------|--------|
| O* | oracle       | oracle   | same   |
| B0 | --           | --       | always SAME_RAIL_RETRY |
| B1 | same detector| oracle   | same   |
| B2 | same detector| L1/L2    | same   |
| B3 | same detector| L1/L2/L3 | same   |

THE DETECTOR AND POLICY ARE IDENTICAL ACROSS B1/B2/B3 (§1.2 policy identity).
Diagnosis is the only varying input, so any difference in recovered value
between them is attributable to diagnosis quality and nothing else. B1, B2 and
B3 are therefore computed from the SAME detection pass -- re-running detection
per arm would let RNG differences leak in and quietly break the pairing that
§12.4's whole analysis rests on.

This module is under src/eval/, so it MAY read the ledger and the efficacy
matrix (§1.2). It is the only place where truth and prediction meet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from ..environment import Efficacies, best_action, value
from ..policy import (GateEvidence, IncidentBudget, apply_bounds, authorize,
                       decide, load_policy)
from ..schema import Action, Diagnosis, TrueEpisode

ARMS = ("O_STAR", "B0", "B1", "B2", "B3")


@dataclass
class ArmOutcome:
    """One arm's result on one scenario."""
    arm: str
    actions: list[str] = field(default_factory=list)
    recovered_paise: float = 0.0
    cost_paise: float = 0.0
    penalty_paise: float = 0.0
    n_interventions: int = 0
    n_bounded: int = 0
    gated: int = 0
    attribution_correct: int = 0
    attribution_applicable: int = 0

    @property
    def net_paise(self) -> float:
        return self.recovered_paise - self.cost_paise - self.penalty_paise

    @property
    def attribution_accuracy(self) -> float | None:
        if not self.attribution_applicable:
            return None
        return self.attribution_correct / self.attribution_applicable


@dataclass(frozen=True)
class IncidentExposure:
    """What one incident put at stake. Assembled by the caller from the stream;
    the arms differ only in what they DO about it.
    """
    incident_id: str
    at_risk_paise: float
    touched_paise: float
    start_window: int
    true_mechanism: str          # "none" for a false alarm on a healthy segment
    true_cause_node: str | None
    # observable-at-decision-time evidence for the production intervention gate
    persistence_windows: int = 999
    max_effect_logodds: float = 99.0


def _apply(arm: str, action: Action, exposure: IncidentExposure,
           eff: Efficacies, out: ArmOutcome, when, budget: IncidentBudget,
           policy_cfg: dict) -> None:
    """Run one action through the bounds, then through the environment."""
    bounded = apply_bounds(action, when, budget, cfg=policy_cfg)
    executed = bounded.action
    if bounded.bounds_fired:
        out.n_bounded += 1

    v = value(exposure.true_mechanism, executed.value,
              exposure.at_risk_paise, exposure.touched_paise, eff)
    out.recovered_paise += v.recovered_paise
    out.cost_paise += v.cost_paise
    out.penalty_paise += v.penalty_paise
    out.actions.append(executed.value)
    if executed is not Action.NO_ACTION:
        out.n_interventions += 1
        budget.actions_taken += 1
        budget.last_action_at = when


def _oracle_diagnosis(exposure: IncidentExposure, incident_id: str) -> Diagnosis:
    """§1.2 oracle scope: the oracle receives the LATENT CAUSE LABEL ONLY.
    Never efficacies. That is what keeps O* and B1 honest -- an oracle that saw
    the efficacy draw would be choosing the best action, not the right
    diagnosis, and the detection/attribution split would stop meaning anything.
    """
    family = (exposure.true_mechanism
              if exposure.true_mechanism not in ("none",)
              and not exposure.true_mechanism.startswith("ood_")
              else None)
    return Diagnosis(
        incident_id=incident_id,
        cause_node=exposure.true_cause_node if family else None,
        mechanism_family=family,
        level_used="ORACLE",
        confidence=1.0,
        l3_eligible=False,
        l3_abstain_reason=None,
        evidence={"oracle": True},
    )


def run_arm(arm: str, exposures: list[IncidentExposure],
            diagnoses: dict[str, Diagnosis], eff: Efficacies,
            epoch, policy_cfg: dict | None = None) -> ArmOutcome:
    """Score one arm over one scenario's incidents.

    `exposures` are the incidents this arm SEES:
      - O* sees the true episodes (oracle detection)
      - B1/B2/B3 see the detector's incidents
      - B0 sees `blind_exposures` -- §12.3 gives B0 a detection column of "--",
        so it must NOT depend on the detector at all
    `diagnoses` maps incident_id -> Diagnosis, and is unused by O*/B0/B1.
    """
    policy_cfg = policy_cfg or load_policy()
    out = ArmOutcome(arm=arm)

    for exp in exposures:
        when = epoch + timedelta(minutes=5 * exp.start_window)
        budget = IncidentBudget()

        if arm == "B0":
            # §12.3: no detection, no cause, always retry the same rail. The
            # "do something reflexive" strawman every recovery system starts as.
            action = Action.SAME_RAIL_RETRY
        elif arm in ("O_STAR", "B1"):
            dx = _oracle_diagnosis(exp, exp.incident_id)
            action = decide(dx, policy_cfg)
            out.attribution_applicable += 1
            out.attribution_correct += 1        # oracle, by construction
        else:
            dx = diagnoses.get(exp.incident_id)
            if dx is None:
                action = Action.NO_ACTION
            else:
                action = decide(dx, policy_cfg)
                if exp.true_mechanism not in ("none",):
                    out.attribution_applicable += 1
                    out.attribution_correct += int(
                        dx.mechanism_family == exp.true_mechanism)
                # PRODUCTION GATE. Only the diagnosis-driven arms pass through
                # it: O*/B1 have an oracle and B0 has no diagnosis at all, so
                # gating them would confuse what the gate is being credited for.
                g = authorize(action, GateEvidence(
                    persistence_windows=exp.persistence_windows,
                    max_effect_logodds=exp.max_effect_logodds,
                    at_risk_paise=int(exp.at_risk_paise),
                    touched_paise=int(exp.touched_paise)), policy_cfg)
                if not g.authorized:
                    out.gated += 1
                    action = Action.NO_ACTION

        _apply(arm, action, exp, eff, out, when, budget, policy_cfg)

    return out


def oracle_optimal(exposures: list[IncidentExposure], eff: Efficacies) -> float:
    """HINDSIGHT action ceiling: the best achievable value if you could see each
    scenario's efficacy DRAW before choosing an action.

    NOT a §12.3 baseline, and NOT policy regret. This reads random per-scenario
    values that no deployable system has access to at decision time, so the gap
    `hindsight_ceiling - O*` bounds what any action rule could gain WITH
    FOREKNOWLEDGE. It belongs beside the decomposition as context, never inside
    it as an error term.

    A genuine policy-regret figure would need a comparator action rule selected
    on dev data before the frozen run. This build has none, so none is quoted.
    (Corrected after the frozen run. See WORKLOG 27 Aug.)
    """
    total = 0.0
    for exp in exposures:
        _, best = best_action(exp.true_mechanism, exp.at_risk_paise,
                              exp.touched_paise, eff)
        total += best
    return total


def blind_exposures(ledger: list[TrueEpisode], episode_amounts: dict,
                    background_at_risk: float, background_touched: float,
                    epoch, first_window: int) -> list[IncidentExposure]:
    """What B0 acts on. SPEC §12.3 gives B0 no detection and no cause: it
    retries the same rail, always, everywhere.

    Wiring B0 to the detector's incidents (as an earlier version did) makes the
    strawman's value move with h, so tuning the detector would silently move the
    bar B0 sets. §10's sensitivity requirement -- "the policy beats B0 in all
    three regimes" -- is only meaningful if B0 is a FIXED comparator.

    Exposures are the true episodes (where a retry can actually recover
    something) plus one covering all remaining failed value in the evaluation
    period, whose mechanism is "none" -- retrying a payment that failed for no
    systemic reason recovers nothing and still costs.
    """
    out = []
    for ep in ledger:
        if ep.mechanism == "none":
            continue
        a, t = episode_amounts.get(ep.episode_id, (0.0, 0.0))
        out.append(IncidentExposure(
            incident_id=f"blind-{ep.episode_id}",
            at_risk_paise=a, touched_paise=t,
            start_window=int((ep.start - epoch).total_seconds() // 300),
            true_mechanism=ep.mechanism,
            true_cause_node=ep.affected_nodes[0] if ep.affected_nodes else None,
        ))
    out.append(IncidentExposure(
        incident_id="blind-background", at_risk_paise=background_at_risk,
        touched_paise=background_touched, start_window=first_window,
        true_mechanism="none", true_cause_node=None))
    return out


def true_exposures(ledger: list[TrueEpisode], at_risk: dict[str, float],
                   touched: dict[str, float], epoch) -> list[IncidentExposure]:
    """Oracle detection: one exposure per TRUE episode, whether or not the
    detector found it. This is what makes O* an upper bound on detection.
    """
    out = []
    for ep in ledger:
        if ep.mechanism == "none":
            continue
        start_w = int((ep.start - epoch).total_seconds() // 300)
        out.append(IncidentExposure(
            incident_id=ep.episode_id,
            at_risk_paise=at_risk.get(ep.episode_id, 0.0),
            touched_paise=touched.get(ep.episode_id, 0.0),
            start_window=start_w,
            true_mechanism=ep.mechanism,
            true_cause_node=ep.affected_nodes[0] if ep.affected_nodes else None,
        ))
    return out
