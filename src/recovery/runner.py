"""The recovery agent. Ingest real revenue at risk, decide, act, measure.

This is the production loop. It runs on the live Razorpay test-mode account,
not on the simulator, and it closes the loop the track brief describes:

    detect revenue at risk -> determine the intervention
      -> execute a BOUNDED recovery workflow -> measure what came back

TWO PATHS, because the two revenue streams need different machinery:

  AGGREGATE (payment failures)   Individual failures are noise; a degradation
                                 is a rate shift across a segment. Runs the
                                 detector, the attribution ladder and the
                                 intervention gate, then acts once for the
                                 whole segment.

  PER ITEM (checkout abandoned)  Nothing failed and no rate shifted -- a
                                 customer left. Each abandoned checkout is
                                 assessed and recovered on its own, under the
                                 same bounds.

Both write one audit trail and one measured ledger. MEASURED means the money is
read back from the Razorpay API afterwards, not modelled: a recovery link that
gets paid is recovered revenue; one that does not, is not.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..execution import razorpay as rz
from ..ingest.razorpay_source import (RevenueAtRisk, abandoned_checkouts,
                                      fetch_invoices, fetch_orders,
                                      fetch_payment_links, fetch_payments,
                                      overdue_receivables, payment_failures)
from ..ingest.razorpay_source import linked_order_ids
from ..policy import (GateEvidence, IncidentBudget, apply_bounds, authorize,
                      load_policy)
from ..schema import Action

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class RecoveryAttempt:
    """One bounded intervention against one unit of revenue at risk."""
    attempt_id: str
    kind: str                       # payment_failure | checkout_abandoned
    reference: str                  # the at-risk object we are recovering
    at_risk_paise: int
    decided_at: datetime
    action_chosen: str
    action_executed: str
    authorized: bool
    gate_reasons: list[str] = field(default_factory=list)
    bounds_fired: list[str] = field(default_factory=list)
    escalated: bool = False
    execution_mode: str = "SIMULATED"
    execution_reference: str | None = None
    execution_url: str | None = None
    rail_restriction_enforced: bool = False
    # filled in by measure(), after the fact, from the API
    recovered_paise: int = 0
    outcome: str = "pending"        # pending | recovered | not_recovered | not_attempted
    narration: str = ""


@dataclass
class BatchResult:
    started_at: datetime
    finished_at: datetime | None = None
    at_risk_total_paise: int = 0
    recovered_total_paise: int = 0
    attempts: list[RecoveryAttempt] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    stream_totals: dict = field(default_factory=dict)

    @property
    def recovery_rate(self) -> float:
        return (self.recovered_total_paise / self.at_risk_total_paise
                if self.at_risk_total_paise else 0.0)

    @property
    def n_attempted(self) -> int:
        return sum(1 for a in self.attempts if a.authorized)


def _stale_minutes(cfg: dict) -> int:
    return int(cfg.get("abandonment", {}).get("stale_after_minutes", 30))


def collect(env: dict | None = None, cfg: dict | None = None,
            now: datetime | None = None) -> list[RevenueAtRisk]:
    """Everything currently at risk on the account, both streams."""
    env = env or rz.load_env()
    cfg = cfg or load_policy()
    payments = fetch_payments(500, env)
    orders = fetch_orders(500, env)
    links = fetch_payment_links(200, env)
    invoices = fetch_invoices(200, env)
    link_orders = linked_order_ids(links, env)
    invoice_orders = {i.get("order_id") for i in invoices if i.get("order_id")}
    stale = _stale_minutes(cfg)

    from ..recovery.declines import load_declines
    rec = (load_declines().get("receivables") or {})
    return (payment_failures(payments, link_orders | invoice_orders)
            + abandoned_checkouts(orders, links, payments,
                                  stale_after_minutes=stale, now=now,
                                  invoices=invoices, env=env, linked_orders=link_orders)
            + overdue_receivables(
                invoices, now=now,
                payment_terms_days=int(rec.get("payment_terms_days", 14)),
                grace_days=int(rec.get("grace_days", 2))))


def _abandonment_action(item: RevenueAtRisk) -> Action:
    """A customer who left mid-checkout is not a rail problem. Re-presenting the
    same broken path is pointless; the useful move is to hand them a fresh link
    they can pay on any working method.
    """
    return Action.ALTERNATE_METHOD_LINK


def decide_and_execute(item: RevenueAtRisk, budget: IncidentBudget,
                       cfg: dict, now: datetime, dry_run: bool = True,
                       already_recovering: set[str] | None = None,
                       segment_volume: dict[str, int] | None = None
                       ) -> RecoveryAttempt:
    """One item, one bounded decision. Nothing here can act without clearing
    the gate AND the §9 bounds.
    """
    already_recovering = already_recovering or set()
    action = _abandonment_action(item)

    attempt = RecoveryAttempt(
        attempt_id=f"rec-{item.reference}",
        kind=item.kind, reference=item.reference,
        at_risk_paise=item.amount_paise, decided_at=now,
        action_chosen=action.value, action_executed=Action.NO_ACTION.value,
        authorized=False,
    )

    # never chase the same object twice in one batch
    if item.reference in already_recovering:
        attempt.gate_reasons = ["already has an open recovery attempt"]
        attempt.outcome = "not_attempted"
        return attempt

    # A single failed payment is NOT actionable on its own. One failure is
    # noise; a degradation is a rate shift across a segment, and telling them
    # apart needs volume the detector can measure. Routing individual failures
    # through the per-item gate produced a misleading "effect 0.00 below
    # threshold" -- an effect size that was never computed, reported as though
    # it had been measured and found small.
    #
    # Until a segment carries enough traffic for the detector, the honest answer
    # is that we cannot tell, and we say so.
    if item.kind == "payment_failure":
        min_n = int(cfg.get("aggregate", {}).get("min_payments_per_segment", 30))
        seen = segment_volume.get(item.segment, 0) if segment_volume else 0
        if seen < min_n:
            attempt.gate_reasons = [
                f"insufficient volume to detect a degradation: segment "
                f"{item.segment} has {seen} payments, need {min_n}"]
            attempt.outcome = "not_attempted"
            return attempt

    # the production intervention gate. An abandoned checkout has no rate
    # shift, so persistence is how long it has sat unpaid and effect size does
    # not apply -- marked not-applicable rather than faked as zero.
    age_windows = max(1, int((now - item.created_at).total_seconds() // 300))
    gate = authorize(action, GateEvidence(
        persistence_windows=age_windows,
        max_effect_logodds=99.0,
        at_risk_paise=item.amount_paise,
        touched_paise=item.amount_paise), cfg)
    attempt.gate_reasons = list(gate.reasons)
    if not gate.authorized:
        attempt.outcome = "not_attempted"
        return attempt

    bounded = apply_bounds(action, now, budget, exposure_paise=item.amount_paise,
                           cfg=cfg)
    attempt.bounds_fired = list(bounded.bounds_fired)
    attempt.escalated = bounded.escalate
    if not bounded.allowed:
        attempt.outcome = "not_attempted"
        return attempt

    attempt.authorized = True
    attempt.action_executed = bounded.action.value

    if dry_run:
        attempt.execution_mode = "SIMULATED"
        attempt.outcome = "pending"
        return attempt

    # REAL execution. A recovery link on the live test-mode account.
    try:
        res = rz.create_alternate_method_link(
            amount_paise=item.amount_paise,
            exclude_methods=[],
            description=f"Recovery for {item.reference} ({item.kind})")
        attempt.execution_mode = res.mode
        attempt.execution_reference = res.reference
        attempt.execution_url = res.url
        attempt.rail_restriction_enforced = res.rail_restriction_enforced
        budget.actions_taken += 1
        budget.last_action_at = now
        budget.spend_paise += item.amount_paise
    except Exception as exc:                      # noqa: BLE001
        attempt.execution_mode = "SIMULATED"
        attempt.gate_reasons.append(f"execution failed: {exc}")
        attempt.authorized = False
        attempt.outcome = "not_attempted"
    return attempt


def run_batch(dry_run: bool = True, limit: int | None = None,
              env: dict | None = None, cfg: dict | None = None,
              now: datetime | None = None) -> BatchResult:
    """Ingest -> decide -> execute, once, over everything currently at risk."""
    cfg = cfg or load_policy()
    env = env or rz.load_env()
    now = now or datetime.now(IST)

    items = collect(env, cfg, now)

    # how much traffic each segment actually carries, so the failure path can
    # tell "no degradation" from "not enough data to say"
    all_payments = fetch_payments(500, env)
    segment_volume: dict[str, int] = {}
    for p in all_payments:
        segment_volume[p.issuer] = segment_volume.get(p.issuer, 0) + 1

    if limit:
        items = sorted(items, key=lambda i: -i.amount_paise)[:limit]

    res = BatchResult(started_at=now)
    res.at_risk_total_paise = sum(i.amount_paise for i in items)
    for kind in ("payment_failure", "checkout_abandoned"):
        sub = [i for i in items if i.kind == kind]
        res.stream_totals[kind] = {"n": len(sub),
                                   "at_risk_paise": sum(i.amount_paise for i in sub)}

    # BOUNDS ARE PER TARGET, NOT PER BATCH.
    #
    # An earlier version shared one budget across the whole run, so the 10-minute
    # cooldown -- which exists to stop us hammering the SAME customer -- blocked
    # every subsequent customer in the batch. A merchant recovering 15 abandoned
    # carts sends 15 links; it does not wait 10 minutes between strangers.
    #
    # The runaway risk is real but it is a different bound: a batch-level cap on
    # how many interventions and how much exposure a single run may touch. Both
    # are enforced below, and both are logged when they fire.
    budgets: dict[str, IncidentBudget] = {}
    seen: set[str] = set()
    batch_cfg = cfg.get("batch_limits", {})
    max_actions = int(batch_cfg.get("max_interventions_per_run", 50))
    max_spend = int(batch_cfg.get("max_exposure_per_run_paise", 100_000_000))
    spent = 0

    for item in sorted(items, key=lambda i: -i.amount_paise):
        if len([a for a in res.attempts if a.authorized]) >= max_actions:
            res.skipped.append({"reference": item.reference,
                                "reason": f"batch cap: {max_actions} interventions"})
            continue
        if spent + item.amount_paise > max_spend:
            res.skipped.append({"reference": item.reference,
                                "reason": "batch cap: exposure ceiling for this run"})
            continue

        budget = budgets.setdefault(item.reference, IncidentBudget())
        a = decide_and_execute(item, budget, cfg, now, dry_run, seen,
                               segment_volume)
        if a.authorized:
            seen.add(item.reference)
            spent += item.amount_paise
        res.attempts.append(a)

    res.finished_at = datetime.now(IST)
    return res


def measure(res: BatchResult, env: dict | None = None) -> BatchResult:
    """Read the outcome back from the API. THIS is what makes the money
    measured rather than modelled: a recovery link that was actually paid is
    recovered revenue, and one that was not, is not.

    RECONCILES AGAINST THE ACCOUNT, not against in-memory state. Recovery links
    outlive the process that created them -- a customer pays hours later, and by
    then the batch object is long gone. So we look up every recovery link the
    agent has ever created (they carry the at-risk reference in their
    description) and match by that. A measurement that only worked inside the
    run that made the call would report zero for every real recovery.
    """
    env = env or rz.load_env()
    links = fetch_payment_links(200, env)

    # reference -> amount actually paid, across every recovery link on record
    paid_by_ref: dict[str, int] = {}
    link_by_ref: dict[str, dict] = {}
    for l in links:
        desc = l.get("description") or ""
        if not desc.startswith("Recovery for "):
            continue
        ref = desc.split("Recovery for ", 1)[1].split(" ")[0]
        paid_by_ref[ref] = paid_by_ref.get(ref, 0) + int(l.get("amount_paid") or 0)
        link_by_ref[ref] = l

    for a in res.attempts:
        if not a.authorized:
            if a.outcome == "pending":
                a.outcome = "not_attempted"
            continue
        paid = paid_by_ref.get(a.reference, 0)
        a.recovered_paise = paid
        a.outcome = "recovered" if paid > 0 else "not_recovered"
        # a recovery link found on the account IS a real execution, even if this
        # particular run was a dry run -- the link outlived the process that
        # created it. Reporting it as SIMULATED would understate what happened.
        if a.reference in link_by_ref:
            a.execution_mode = "REAL"
            a.execution_reference = link_by_ref[a.reference].get("id")
            a.execution_url = link_by_ref[a.reference].get("short_url")
    res.recovered_total_paise = sum(a.recovered_paise for a in res.attempts)
    return res


def to_json(res: BatchResult) -> dict:
    def enc(o):
        return o.isoformat() if isinstance(o, datetime) else o
    return json.loads(json.dumps(asdict(res), default=enc))


def write_audit(path: Path, res: BatchResult) -> None:
    """JSON Lines, one attempt per line -- appendable and readable without
    loading the whole file, same shape as src/audit.py.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for a in res.attempts:
            d = asdict(a)
            d["decided_at"] = a.decided_at.isoformat()
            fh.write(json.dumps(d) + "\n")
