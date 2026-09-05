"""Failed recurring charges, read from Razorpay Subscriptions.

THIS CANNOT RUN ON THIS ACCOUNT, and that is recorded rather than worked
around. `GET /subscriptions` and `GET /plans` both return 401 Unauthorized on
this key while `/payments`, `/orders`, `/invoices` and `/customers` all
succeed -- feature gating, not a credentials problem. Re-probed 1 Sep 2026 and
still 401.

So why write the ingest at all. Because the alternative shapes to leaving a
gap are both worse: quietly dropping the workflow, which hides the most
valuable loop in the system, or demonstrating it with data invented at the
ingest boundary, which makes a capability gap look like a working feature. This
module reads the real API, and raises a typed, specific error when it cannot,
so the day Subscriptions is enabled the loop starts working with no other
change. The simulated subscription book lives in `src/sim/`, clearly labelled,
where every other simulated thing in this project lives.

WHICH SUBSCRIPTIONS ARE AT RISK. Not the failed payments underneath them --
those already arrive through `razorpay_source` and would be counted twice. A
subscription is at risk when the SUBSCRIPTION is, which Razorpay expresses as
status rather than as an event:

    pending    a charge failed; Razorpay is retrying on its own schedule
    halted     those retries are exhausted and billing has stopped

`halted` is the expensive one. Nothing further happens to it without
intervention, and every cycle that passes is revenue that never bills.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..execution.razorpay import _call, load_env
from .razorpay_source import RevenueAtRisk

IST = timezone(timedelta(hours=5, minutes=30))

# The statuses that mean money is not being collected. `active` bills fine and
# `cancelled` / `completed` / `expired` are finished -- chasing either would be
# the subscription equivalent of dunning a captured payment.
AT_RISK_STATUS = ("pending", "halted")

PAGE = 100


class SubscriptionsUnavailable(RuntimeError):
    """The Subscriptions product is not available on this account.

    Typed rather than a bare RuntimeError so a caller can tell "this merchant
    does not have the feature" -- a fact to report -- apart from "the API call
    broke", which is a fault to retry.
    """


@dataclass(frozen=True)
class Subscription:
    """One recurring billing relationship, as Razorpay describes it."""
    subscription_id: str
    plan_id: str
    customer_id: str | None
    status: str
    amount_paise: int
    created_at: datetime
    charge_at: datetime | None
    paid_count: int
    remaining_count: int
    error_reason: str | None
    customer_ref: str | None

    @property
    def at_risk(self) -> bool:
        return self.status in AT_RISK_STATUS

    @property
    def lifetime_at_risk_paise(self) -> int:
        """What stopping now actually costs.

        A halted subscription does not lose one cycle, it loses every cycle
        that remains -- which is the entire argument for why recovering a
        subscription is worth more than recovering a one-off payment of the
        same size. Reported separately from `amount_paise` because the exposure
        floor is applied to the cycle in front of us, not to a projection.
        """
        return self.amount_paise * max(self.remaining_count, 1)


def _available(env: dict) -> None:
    try:
        _call("GET", "/subscriptions?count=1", None, env)
    except RuntimeError as e:
        if "401" in str(e):
            raise SubscriptionsUnavailable(
                "Razorpay Subscriptions is not enabled on this account "
                "(401 from /subscriptions on a key that reads /payments and "
                "/customers fine). Enable the Subscriptions product on the "
                "account; no code change is needed.") from None
        raise


def _plan_amounts(env: dict, plan_ids: set[str]) -> dict[str, int]:
    """Plan id -> amount in paise.

    Fetched per plan rather than per subscription: a book of five hundred
    subscriptions usually runs on a handful of plans, and asking the API five
    hundred times for the same three answers is how the rate limiter gets hit.
    """
    out: dict[str, int] = {}
    for pid in sorted(plan_ids):
        if not pid:
            continue
        try:
            p = _call("GET", f"/plans/{pid}", None, env)
        except RuntimeError:
            continue
        out[pid] = int(((p.get("item") or {}).get("amount")) or 0)
    return out


def fetch_subscriptions(limit: int = 200,
                        env: dict | None = None) -> list[Subscription]:
    """Every subscription on the account, with its plan amount resolved."""
    env = env or load_env()
    _available(env)

    items, skip = [], 0
    while len(items) < limit:
        count = min(PAGE, limit - len(items))
        r = _call("GET", f"/subscriptions?count={count}&skip={skip}", None, env)
        batch = r.get("items", [])
        items.extend(batch)
        if len(batch) < count:
            break
        skip += count

    amounts = _plan_amounts(env, {i.get("plan_id") for i in items})
    out: list[Subscription] = []
    for i in items:
        notes = i.get("notes") or {}
        out.append(Subscription(
            subscription_id=i.get("id", ""),
            plan_id=i.get("plan_id", ""),
            customer_id=i.get("customer_id"),
            status=i.get("status", ""),
            amount_paise=amounts.get(i.get("plan_id"), 0),
            created_at=datetime.fromtimestamp(i.get("created_at", 0), IST),
            charge_at=(datetime.fromtimestamp(i["charge_at"], IST)
                       if i.get("charge_at") else None),
            paid_count=int(i.get("paid_count") or 0),
            remaining_count=int(i.get("remaining_count") or 0),
            # Razorpay does not attach a decline reason to the subscription
            # itself; it belongs to the failed payment underneath. Carried
            # through when a caller has it, UNKNOWN when not -- which routes to
            # reconcile-first rather than guessing a retry schedule.
            error_reason=notes.get("error_reason"),
            customer_ref=notes.get("customer_ref") or notes.get("email"),
        ))
    return out


def to_revenue_at_risk(subs: list[Subscription]) -> list[RevenueAtRisk]:
    """The at-risk ones, in the shape the sequencer already understands."""
    out = []
    for s in subs:
        if not s.at_risk or s.amount_paise <= 0:
            continue
        out.append(RevenueAtRisk(
            kind="subscription_failure",
            reference=s.subscription_id,
            created_at=s.charge_at or s.created_at,
            amount_paise=s.amount_paise,
            segment=s.plan_id or "UNATTRIBUTED",
            method=None,
            detail={"mandate": True,      # a subscription IS a standing mandate
                    "error_reason": s.error_reason,
                    "customer_ref": s.customer_ref,
                    "status": s.status,
                    "paid_count": s.paid_count,
                    "remaining_count": s.remaining_count,
                    "lifetime_at_risk_paise": s.lifetime_at_risk_paise},
        ))
    return out


LOCAL_BOOK = Path("data/live/acme_subs.subscriptions.jsonl")


def read_local(path: Path | None = None) -> list[RevenueAtRisk]:
    """A subscription book this project holds itself.

    Written by `scripts.seed_subscriptions` and served by the `local` gateway,
    because Subscriptions cannot be enabled on this Razorpay account. The rows
    carry everything the sequencer needs and one thing it does not read:
    `funds_return_at`, which is the gateway's business and decides whether a
    silent re-present captures.
    """
    p = Path(path or LOCAL_BOOK)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("status") not in AT_RISK_STATUS:
            continue
        out.append(RevenueAtRisk(
            kind="subscription_failure",
            reference=r["id"],
            created_at=datetime.fromisoformat(r["failed_at"]),
            amount_paise=int(r["amount_paise"]),
            segment=r.get("plan_id") or "UNATTRIBUTED",
            method=None,
            detail={"mandate": bool(r.get("mandate_live", True)),
                    "error_reason": r.get("error_reason"),
                    "customer_ref": r.get("customer_ref"),
                    "status": r.get("status"),
                    "paid_count": r.get("paid_count"),
                    "remaining_count": r.get("remaining_count"),
                    "source": "local",
                    "lifetime_at_risk_paise":
                        int(r["amount_paise"])
                        * max(int(r.get("remaining_count") or 1), 1)},
        ))
    return out


def collect(limit: int = 200, env: dict | None = None,
            allow_local: bool = True) -> list[RevenueAtRisk]:
    """Ingest, or say precisely why not. Never returns an empty list silently.

    Tries the real API first and always will, so the day Subscriptions is
    enabled this switches back with no code change. Falls back to the local
    book rather than to nothing, because a workflow with no data cannot be
    told apart from a workflow that is broken -- which is the confusion that
    made this loop look like a defect for a week.
    """
    try:
        return to_revenue_at_risk(fetch_subscriptions(limit, env))
    except SubscriptionsUnavailable:
        if not allow_local:
            raise
        local = read_local()
        if not local:
            raise
        return local
