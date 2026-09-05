"""What chasing a case actually costs, and where the floor therefore sits.

TWO QUESTIONS THAT KEPT GETTING CONFLATED, and both were answered wrongly.

FIRST: "would chasing this lose money?" The floor was Rs 5,000, justified in
config, in the console and in two documents as the point below which "a recovery
attempt costs more than it can bring back". Nobody had checked. An SMS costs 20
paise; the dearest ladder is 460. At a 30% recovery rate the cash break-even is
tens of rupees, not five thousand -- wrong by more than two orders of magnitude.

SECOND: "is this worth interrupting someone about?" That is the real reason for
a floor above break-even, and for a while it was answered with four round
numbers -- Rs 200 / 500 / 1,000 / 1,500 -- sitting at 2x, 5x, 12x and 750x their
own break-even. Four unrelated figures with prose written after the fact. It
took the question "why is 500 the threshold?" to notice that nothing derived any
of them.

SO THE FLOOR IS NOW DERIVED:

    cash floor   = (messaging + operator time) / p
    policy floor = (cash + contacts x price-of-an-interruption) / p

The price of an interruption is the one judgement left, and it is a far better
thing to expose than a floor. It has a unit. It is comparable across workflows
-- is an unsolicited nudge really six times costlier than an invoice reminder?
-- so a merchant can disagree with it specifically. And it makes the floor move
correctly: add a rung to a ladder and the floor rises, because you are asking
the customer for more patience than before.

It cannot be measured from this account, and that is stated rather than hidden.
What it buys is that the argument is now about a quantity someone can hold an
opinion about, instead of about a round number nobody can.
"""
from __future__ import annotations

from dataclasses import dataclass

from .channels import get as get_channel
from .channels import load_workflows


@dataclass(frozen=True)
class Economics:
    workflow: str
    ladder_cost_paise: int          # messaging, if every rung runs
    human_cost_paise: int           # an operator's time, if it escalates
    total_cost_paise: int           # cash only
    contacts: int
    nuisance_per_contact_paise: int
    nuisance_cost_paise: int        # goodwill spent, priced
    full_cost_paise: int            # cash + goodwill
    recovery_probability: float
    cash_floor_paise: int           # below this, chasing loses MONEY
    policy_floor_paise: int         # below this, it is not worth the interruption
    policy_reason: str

    @property
    def economic_floor_paise(self) -> int:
        """Kept for callers that want the cash-only break-even."""
        return self.cash_floor_paise

    @property
    def gap(self) -> float:
        """How many times higher the chosen floor is than the computed one."""
        return (self.policy_floor_paise / self.economic_floor_paise
                if self.economic_floor_paise else float("inf"))

    def expected_value_paise(self, at_risk_paise: int) -> int:
        return int(at_risk_paise * self.recovery_probability
                   - self.total_cost_paise)


def _human_cost(cfg: dict, channels_used: list[str]) -> int:
    """An operator's time, counted only when the ladder actually reaches one.

    This is the one genuinely non-trivial cost in the model, and it is still
    small: a few minutes of attention, not a few thousand rupees. Counting it
    honestly is what shows that even WITH it the economic floor stays low.
    """
    c = (cfg.get("costs") or {})
    if "human_review" not in channels_used:
        return 0
    minutes = float(c.get("human_review_minutes", 5))
    rate = float(c.get("operator_hourly_paise", 30000))    # Rs 300/hour
    return int(minutes / 60 * rate)


def economics_for(workflow_key: str, schedule_steps: list[dict],
                  recovery_probability: float, nuisance_per_contact: int,
                  policy_reason: str, contacting: set[str],
                  wf_cfg: dict | None = None) -> Economics:
    """Both floors, derived rather than chosen.

        cash floor    = cash cost / p
        policy floor  = (cash cost + contacts x nuisance) / p

    The only judgement left is what one interruption is worth, and that is a
    quantity a merchant can argue about. A round floor is not.
    """
    wf_cfg = wf_cfg or load_workflows()
    chans = [s["channel"] for s in schedule_steps]
    messaging = sum(get_channel(c, wf_cfg).cost_paise for c in chans)
    human = _human_cost(wf_cfg, chans)
    cash = messaging + human
    n_contacts = sum(1 for c in chans if c in contacting)
    nuisance = n_contacts * nuisance_per_contact
    p = recovery_probability or 1.0
    return Economics(
        workflow=workflow_key,
        ladder_cost_paise=messaging,
        human_cost_paise=human,
        total_cost_paise=cash,
        contacts=n_contacts,
        nuisance_per_contact_paise=nuisance_per_contact,
        nuisance_cost_paise=nuisance,
        full_cost_paise=cash + nuisance,
        recovery_probability=recovery_probability,
        cash_floor_paise=int(cash / p),
        policy_floor_paise=int((cash + nuisance) / p),
        policy_reason=policy_reason,
    )


def for_workflow(wf, declines_cfg: dict, wf_cfg: dict | None = None
                 ) -> Economics:
    wf_cfg = wf_cfg or load_workflows()
    costs = wf_cfg.get("costs") or {}
    steps = declines_cfg["schedules"][wf.schedule]["steps"]
    contacting = set(declines_cfg["compliance"]["contacting_channels"])
    nuisance = int((costs.get("nuisance_paise_per_contact") or {})
                   .get(wf.key, 0))
    reason = str((declines_cfg.get("policy_floors") or {})
                 .get(wf.key, {}).get("reason", "no reason recorded"))
    return economics_for(
        wf.key, steps,
        float(costs.get("assumed_recovery_probability", 0.30)),
        nuisance, reason, contacting, wf_cfg)


def floor_for(kind: str, declines_cfg: dict, wf_cfg: dict | None = None) -> int:
    """The floor that actually applies to a case of this kind."""
    from .workflows import for_kind
    wf = for_kind(kind, wf_cfg)
    if wf is None:
        return int(declines_cfg.get("stopping", {}).get("min_at_risk_paise", 0))
    return for_workflow(wf, declines_cfg, wf_cfg).policy_floor_paise
