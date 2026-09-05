"""Hidden action-effect environment. SPEC §10, §10.1.

THIS IS THE COMPONENT THAT MAKES THE THESIS MEASURABLE (§10). It is keyed by
TRUE MECHANISM x EXECUTED ACTION -- the full cross product -- because when B2
misdiagnoses a PSP degradation as an issuer degradation, the policy executes
ALTERNATE_METHOD_LINK and attribution regret is undefined unless we know the
consequence of THAT action under the TRUE mechanism.

Efficacies are sampled per scenario from frozen ranges the attribution and
policy layers never see (§1.2). Nothing here may be imported by
src/attribution/, src/detector/ or src/policy.py; tests/test_leakage.py
enforces it by module name.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config"

KNOWN_MECHANISMS = ("issuer_degradation", "psp_degradation",
                    "card_auth_spike", "upi_network_degradation")


def load_environment() -> dict:
    return yaml.safe_load((CONFIG / "environment.yaml").read_text(encoding="utf-8"))


def mechanism_key(mechanism: str) -> str:
    """Map a ledger mechanism onto an efficacy row.

    Every `ood_*` variant shares the `ood` row on purpose: the attributor was
    never designed for them, so distinguishing their efficacies would imply a
    knowledge we do not claim to have.
    """
    if mechanism in KNOWN_MECHANISMS or mechanism == "none":
        return mechanism
    return "ood"


@dataclass(frozen=True)
class Efficacies:
    """One scenario's draw. Frozen for the life of the scenario so every
    baseline faces the SAME world -- otherwise B2 and B3 would be compared
    across different environments and the paired design would be broken.
    """
    recovery: dict[str, dict[str, float]]      # mechanism -> action -> p
    cost: dict[str, float]                     # action -> added abandonment
    cost_base: dict[str, str]                  # action -> "at_risk" | "touched"
    contact_penalty_paise: int
    contacting: tuple[str, ...]
    regime: str = "base"

    def p_recover(self, mechanism: str, action: str) -> float:
        return self.recovery[mechanism_key(mechanism)][action]

    def action_cost(self, action: str) -> float:
        return self.cost[action]

    def is_contacting(self, action: str) -> bool:
        return action in self.contacting


def sample_efficacies(seed: int, regime: str = "base",
                      cfg: dict | None = None) -> Efficacies:
    """Draw one scenario's efficacies from the frozen ranges.

    `regime` applies the §10 sensitivity sweep. The result counts only if the
    policy beats B0 under pessimistic, base AND optimistic.
    """
    cfg = cfg or load_environment()
    rng = np.random.default_rng(seed)
    scales = cfg["sensitivity"][regime]

    recovery: dict[str, dict[str, float]] = {}
    # sorted() everywhere so the draw order -- and therefore the scenario's
    # whole environment -- is reproducible from the seed alone
    for mech in sorted(cfg["recovery_probability"]):
        row = cfg["recovery_probability"][mech]
        recovery[mech] = {
            act: float(np.clip(rng.uniform(*row[act]) * scales["recovery_scale"],
                               0.0, 1.0))
            for act in sorted(row)
        }

    cost = {act: float(np.clip(rng.uniform(*cfg["action_cost"][act])
                               * scales["cost_scale"], 0.0, 1.0))
            for act in sorted(cfg["action_cost"])}

    return Efficacies(recovery=recovery, cost=cost,
                      cost_base=dict(cfg["cost_base"]),
                      contact_penalty_paise=int(cfg["contact_penalty_paise"]),
                      contacting=tuple(cfg["customer_contacting"]),
                      regime=regime)


# ------------------------------------------------------------------ §10.1

@dataclass(frozen=True)
class ValueBreakdown:
    recovered_paise: float
    cost_paise: float
    penalty_paise: float

    @property
    def net_paise(self) -> float:
        return self.recovered_paise - self.cost_paise - self.penalty_paise


def value(mechanism: str, action: str, at_risk_paise: float,
          touched_paise: float, eff: Efficacies) -> ValueBreakdown:
    """Simulated recovered value for one executed action. SPEC §10.1.

        value = at_risk  x p_recover(true_mechanism, action)
              - touched  x cost(action)
              - contact_penalty x 1[action contacts the customer]

    Which volume the cost is charged against is per-action (`cost_base` in
    environment.yaml), because the four actions are not equally intrusive.
    SWITCH_PSP reroutes the entire flow, so its added abandonment falls on
    customers who would have paid fine; the other three touch only payments
    that already failed. Charging every action against the full flow made
    NO_ACTION optimal 61-88% of the time -- see that config comment.

    A false intervention stays genuinely costly either way: on a healthy
    segment there is nothing to recover, and SWITCH_PSP still pays against the
    whole flow while the customer-contacting actions still pay the contact
    penalty. "An agent that always acts is dangerous" (§10) remains visible in
    rupees, which is the point of the `none` row.
    """
    p = eff.p_recover(mechanism, action)
    recovered = float(at_risk_paise) * p
    base = (float(touched_paise) if eff.cost_base.get(action) == "touched"
            else float(at_risk_paise))
    cost = base * eff.action_cost(action)
    penalty = float(eff.contact_penalty_paise) if eff.is_contacting(action) else 0.0
    if action == "NO_ACTION":
        # NO_ACTION touches nothing and contacts nobody. Some payments still
        # recover on their own, which is why it has a recovery column at all.
        cost = penalty = 0.0
    return ValueBreakdown(recovered, cost, penalty)


def best_action(mechanism: str, at_risk_paise: float, touched_paise: float,
                eff: Efficacies) -> tuple[str, float]:
    """The value-maximising action given the TRUE mechanism AND this scenario's
    DRAWN efficacies.

    *** THIS IS A HINDSIGHT ORACLE. NOT A POLICY. ***

    It reads the per-scenario efficacy draw -- random values no deployable
    system could know in advance -- and retrospectively picks whichever action
    happened to pay best. The gap between this and O* is therefore a HINDSIGHT
    ACTION-SELECTION GAP, an upper bound on what any action rule could achieve
    with foreknowledge of the draws.

    It is NOT "policy regret". Policy regret would require a comparator policy
    chosen on dev data BEFORE the frozen evaluation, using only information
    available at decision time. No such comparator exists in this build, so no
    policy-regret figure should be quoted.
    (Corrected after the frozen run; an earlier docstring claimed otherwise and
    RESULTS.md quoted it as policy regret. See WORKLOG 27 Aug.)
    """
    scores = {a: value(mechanism, a, at_risk_paise, touched_paise, eff).net_paise
              for a in sorted(eff.cost)}
    best = max(scores, key=lambda a: (scores[a], a))
    return best, scores[best]


def optimal_action_flips(cfg: dict | None = None, n: int = 400) -> dict:
    """§10 falsifiability check: the ranges must overlap enough that the optimal
    action genuinely flips between draws, and ALTERNATE_METHOD_LINK's higher
    cost must sometimes make acting worse than waiting.

    Verified on the DEV pool, never by inspecting the frozen holdout (§13).
    Returns the distribution of optimal actions per mechanism so the check is
    reported rather than asserted in passing.
    """
    cfg = cfg or load_environment()
    out: dict[str, dict[str, int]] = {}
    for mech in KNOWN_MECHANISMS:
        counts: dict[str, int] = {}
        for i in range(n):
            eff = sample_efficacies(seed=100_000 + i, cfg=cfg)
            # a realistic incident: some money failing inside a much larger flow
            a, _ = best_action(mech, at_risk_paise=1_500_000,
                               touched_paise=12_000_000, eff=eff)
            counts[a] = counts.get(a, 0) + 1
        out[mech] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return out
