"""Does the loop work? A different question from: can this account feed it.

`workflows.readiness` answers one question -- is there enough live data, and is
the product enabled -- and reports a single BLOCKED with nothing beside it. For
two of the four loops that reads as "broken", and it is not what is true. The
degradation detector is starved of volume; the subscription loop is gated at
401. Neither is a statement about whether the code works.

So this module asks the second question directly, by RUNNING each loop on a
stream it can actually be run on and reporting what came out. The distinction
matters to anyone deciding whether to trust the system:

    blocked on data   the loop works; this account cannot feed it
    broken            the loop does not work

Those need different responses -- one is a dashboard setting or a busier
merchant, the other is a bug -- and a single red label cannot tell you which
you have.

EVERY CHECK RUNS THE PRODUCTION CODE. Nothing here re-implements a detector or
a ladder. The degradation check calls `degradation.scan`; the subscription
check opens real sequences through `campaign.run_pass` against the real
schedules and the real guards. The only thing supplied is the input, and where
that input is simulated the result says so in the word `SIMULATED`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

IST = timezone(timedelta(hours=5, minutes=30))

LIVE = "LIVE"
SIMULATED = "SIMULATED"


@dataclass
class LoopResult:
    """What happened when the loop was actually run."""
    workflow: str
    ran: bool
    source: str = SIMULATED
    headline: str = ""
    detail: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def status(self) -> str:
        if self.error:
            return "BROKEN"
        return "works" if self.ran else "not run"


def check_degradation(seed: int = 7) -> LoopResult:
    """Detect an incident, name its cause, choose an intervention.

    The scan is not told which segment is degrading, when it starts, or that
    anything is wrong at all. `traffic.generate` returns the ground truth
    separately so the answer can be graded afterwards.
    """
    from src.recovery.degradation import scan
    from src.sim import traffic as T
    try:
        pays, truth = T.generate(minutes=240, per_minute=9.0, seed=seed)
        res = scan(pays)
        if not res.incident:
            return LoopResult("payment_degradation", False, SIMULATED,
                              "no incident detected in a stream that has one",
                              error="detector missed an injected incident")
        found = [f"{c.segment}/{c.method}" for c in res.alerting_cells]
        hit = f"{truth.segment}/{truth.method}" in found
        d = res.diagnosis
        return LoopResult(
            "payment_degradation", True, SIMULATED,
            f"found {', '.join(found)} -> {d.mechanism_family} -> {res.action.name}",
            detail=[
                f"injected   {truth.describe()}  (never shown to the scan)",
                f"detected   {', '.join(found)}"
                + ("" if hit else "   MISS: not the injected cell"),
                f"cause      {d.cause_node}",
                f"mechanism  {d.mechanism_family}   confidence "
                f"{d.confidence:.2f} ({d.level_used})",
                f"action     {res.action.name}  -- a reroute, not a message",
                f"{len(pays)} payments, baseline {res.baseline_rate:.1%}, "
                f"largest cell {res.largest_cell}",
            ])
    except Exception as e:                               # noqa: BLE001
        return LoopResult("payment_degradation", False, SIMULATED,
                          error=f"{type(e).__name__}: {e}"[:200])


def check_subscription(n: int = 40, days: int = 21,
                       seed: int = 20260829) -> LoopResult:
    """Open real sequences on the mandate ladder and see what they recover.

    Runs the production `campaign.run_pass` against the real `mandate`
    schedule, the real guards and a gateway that holds a mandate -- which is
    the one capability this Razorpay account lacks and the whole reason the
    loop cannot run live. Everything else is the code that runs in production.
    """
    import numpy as np

    from src.recovery import channels, ledger as L
    from src.policy import load_policy
    from src.recovery.campaign import classify_item, run_pass
    from src.recovery.declines import load_declines
    from src.sim import book as B
    from src.sim import customer as C
    from src.sim.gateway import SimGateway
    try:
        cfg, wf_cfg, policy = load_declines(), channels.load_workflows(), load_policy()
        rng = np.random.default_rng(seed)
        start = datetime(2026, 9, 1, 9, 0, tzinfo=IST)

        # Subscriptions only. A mixed book would work too, but the point of
        # this check is the mandate ladder, and mixing in streams that cannot
        # use it would bury the result the reader is looking for.
        items = [i for i in B.generate(n * 8, start, seed=seed, cfg=cfg)
                 if i.kind == "subscription_failure"][:n]
        if not items:
            return LoopResult("subscription_failure", False, SIMULATED,
                              error="no subscription cases generated")

        personas, amounts = {}, {}
        for it in items:
            cls = classify_item(it, cfg).decline_class
            personas[it.reference] = C.draw_persona(it.reference, cls, rng)
            amounts[it.reference] = it.amount_paise

        from scripts.recovery_curve import BOOK_A, NO_AUTOMATION, fit_p
        p_base = fit_p(NO_AUTOMATION[0], BOOK_A)

        def outcome(ref: str, attempt_no: int) -> bool:
            p = personas.get(ref)
            return bool(p and C.mandate_charge(p, amounts.get(ref, 0),
                                               p_base, rng))

        gw = SimGateway(mandate=True, mandate_outcome=outcome, clock=start)
        with TemporaryDirectory() as tmp:
            t = Path(tmp)
            paths = dict(path=t / "l.jsonl", sup_path=t / "s.jsonl",
                         notif_path=t / "n.jsonl", promise_path=t / "p.jsonl")
            now, end = start, start + timedelta(days=days)
            while now <= end:
                gw.clock = now
                run_pass([i for i in items if i.created_at <= now], now, cfg,
                         dry_run=False, env={}, gateway=gw, wf_cfg=wf_cfg,
                         policy=policy, deliver={"sms": True, "email": True},
                         **paths)
                now += timedelta(hours=1)

            rows = L.read(paths["path"])
            won = {e["reference"] for e in rows
                   if e["event"] == L.ATTEMPT_SUCCEEDED}
            opened = {e["reference"] for e in rows
                      if e["event"] == L.SEQUENCE_OPENED}
            money = sum(int(e.get("amount_paise") or 0) for e in rows
                        if e["event"] == L.ATTEMPT_SUCCEEDED)
            at_risk = sum(amounts[r] for r in opened)
            silent = len(gw.charges)
            captured = sum(1 for c in gw.charges if c["paid"])
            contacts = sum(1 for e in rows
                           if e["event"] == L.ATTEMPT_DELIVERED)

        rate = len(won) / len(opened) if opened else 0.0
        return LoopResult(
            "subscription_failure", True, SIMULATED,
            f"recovered {len(won)} of {len(opened)} ({rate:.0%}), "
            f"{silent} silent retries, {contacts} contacts",
            detail=[
                f"opened     {len(opened)} on the 7-step mandate ladder",
                f"silent     {silent} re-presents, {captured} captured, "
                f"0 customers contacted by them",
                f"contacts   {contacts} messages -- the ladder only reaches "
                f"for one after four silent tries",
                f"recovered  Rs {money/100:,.0f} of Rs {at_risk/100:,.0f}  "
                f"({money/at_risk:.0%})" if at_risk else "",
                "the silent rungs are what this account cannot do; "
                "docs/RECOVERY_RATE.md puts them at 19 points of recovery rate",
            ])
    except Exception as e:                               # noqa: BLE001
        return LoopResult("subscription_failure", False, SIMULATED,
                          error=f"{type(e).__name__}: {e}"[:200])


def check_live_loop(key: str, items: list) -> LoopResult:
    """For the two loops that already run on this account, say so."""
    mine = [i for i in items if i.kind == {
        "checkout_abandonment": "checkout_abandoned",
        "invoice_overdue": "overdue_receivable"}.get(key, key)]
    amount = sum(i.amount_paise for i in mine)
    return LoopResult(key, bool(mine), LIVE,
                      f"running on {len(mine)} live case(s), "
                      f"Rs {amount/100:,.0f} at risk")


CHECKS = {
    "payment_degradation": check_degradation,
    "subscription_failure": check_subscription,
}


def check(key: str, items: list | None = None) -> LoopResult:
    fn = CHECKS.get(key)
    return fn() if fn else check_live_loop(key, items or [])
