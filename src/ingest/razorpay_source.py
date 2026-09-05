"""Real Razorpay data ingestion.

Everything upstream of this module used a simulator. This reads the actual
account -- payments, orders and payment links -- over the test-mode API and
normalises them into the same window counts the detector already consumes, so
the identical pipeline runs on real data with nothing swapped out.

TWO REVENUE STREAMS, per the track brief:

  payment failure     a payment reached `failed`, with a real Razorpay
                      error_code / error_source / error_step / error_reason.
  checkout abandoned  an order or payment link was created and never paid.
                      This is revenue at risk that no failure event ever
                      reports, because nothing failed -- the customer left.

Test mode only. `execution/razorpay.py` refuses a non-test key and this module
uses the same guard.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..execution.razorpay import RECOVERY_TAG
from pathlib import Path

from ..execution.razorpay import _call, load_env

IST = timezone(timedelta(hours=5, minutes=30))
WINDOW_SECONDS = 300
PAGE = 100

# The LIVE API does not always return the vocabulary the per-method docs table
# lists. Verified against real test-mode failures on 27 Aug:
#
#   netbanking failure  ->  source = "bank"      (docs table says issuer_bank)
#   wallet failure      ->  source = "customer"
#
# Normalising here, at the ingestion seam, is the right place: everything
# downstream -- signatures.yaml, the partition selector, the policy -- keys on
# one canonical vocabulary, and only this module has to know that the wire
# format differs. Without it, attribution silently never matches a real
# netbanking failure, which is the kind of defect that only surfaces against a
# live account.
SOURCE_ALIASES = {
    "bank": "issuer_bank",
    "issuer": "issuer_bank",
    "wallet": "customer_psp",
}


def canonical_source(raw: str | None) -> str | None:
    if raw is None:
        return None
    return SOURCE_ALIASES.get(raw, raw)


# --------------------------------------------------------------- raw records

@dataclass(frozen=True)
class RealPayment:
    payment_id: str
    created_at: datetime
    amount_paise: int
    status: str                    # created | authorized | captured | failed | refunded
    method: str | None
    bank: str | None
    wallet: str | None
    vpa: str | None
    error_code: str | None
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    order_id: str | None
    email: str | None = None
    contact: str | None = None

    @property
    def customer_ref(self) -> str | None:
        """Whatever identity we can suppress on. Email first -- it is stable,
        and a phone number is reassigned more often than an address is."""
        return self.email or self.contact

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def succeeded(self) -> bool:
        return self.status in ("captured", "authorized")

    @property
    def issuer(self) -> str:
        """Best available issuer identity. Real accounts expose the bank on
        netbanking and the card issuer on cards; UPI exposes the handle. Where
        Razorpay gives us nothing we say so rather than inventing a segment.
        """
        if self.bank:
            return self.bank
        if self.vpa and "@" in self.vpa:
            return self.vpa.split("@", 1)[1].upper()
        if self.wallet:
            return self.wallet.upper()
        return "UNATTRIBUTED"


@dataclass(frozen=True)
class RevenueAtRisk:
    """One unit of revenue at risk, whatever produced it."""
    kind: str                      # "payment_failure" | "checkout_abandoned"
    reference: str                 # payment_id / order_id / plink_id
    created_at: datetime
    amount_paise: int
    segment: str                   # issuer / bank / handle, or UNATTRIBUTED
    method: str | None
    detail: dict = field(default_factory=dict)


# ------------------------------------------------------------------ fetching

def _paged(path: str, env: dict, limit: int, extra: str = "") -> list[dict]:
    """Razorpay list endpoints are skip/count paginated."""
    items, skip = [], 0
    while len(items) < limit:
        count = min(PAGE, limit - len(items))
        sep = "&" if extra else ""
        r = _call("GET", f"{path}?count={count}&skip={skip}{sep}{extra}", None, env)
        batch = r.get("items", [])
        items.extend(batch)
        if len(batch) < count:
            break
        skip += count
        time.sleep(0.15)           # be polite to the sandbox
    return items


def fetch_payments(limit: int = 500, env: dict | None = None) -> list[RealPayment]:
    env = env or load_env()
    out = []
    for p in _paged("/payments", env, limit):
        out.append(RealPayment(
            payment_id=p.get("id", ""),
            created_at=datetime.fromtimestamp(p.get("created_at", 0), IST),
            amount_paise=int(p.get("amount") or 0),
            status=p.get("status", ""),
            method=p.get("method"),
            bank=p.get("bank"),
            wallet=p.get("wallet"),
            vpa=p.get("vpa"),
            error_code=p.get("error_code"),
            error_source=canonical_source(p.get("error_source")),
            error_step=p.get("error_step"),
            error_reason=p.get("error_reason"),
            order_id=p.get("order_id"),
            email=p.get("email"),
            contact=p.get("contact"),
        ))
    return out


def fetch_orders(limit: int = 500, env: dict | None = None) -> list[dict]:
    return _paged("/orders", env or load_env(), limit)


def fetch_payment_links(limit: int = 200, env: dict | None = None) -> list[dict]:
    """Payment Links does NOT share the list shape of /payments and /orders --
    it returns `payment_links` rather than `items` and ignores skip/count.
    Handled separately rather than forced through `_paged`, which silently
    returned zero links.
    """
    r = _call("GET", "/payment_links", None, env or load_env())
    return (r.get("payment_links") or r.get("items") or [])[:limit]


# --------------------------------------------------------- revenue at risk

def payment_failures(payments: list[RealPayment], excluded_orders: set | None = None) -> list[RevenueAtRisk]:
    settled = {p.order_id for p in payments if (p.succeeded or p.status == "refunded") and p.order_id}
    # Recover an obligation once, not once per failed attempt against it.
    latest = {}
    for p in sorted(payments, key=lambda p: p.created_at):
        if p.failed and p.order_id not in settled and p.order_id not in (excluded_orders or set()):
            latest[p.order_id or p.payment_id] = p
    return [RevenueAtRisk(
        kind="payment_failure", reference=p.payment_id, created_at=p.created_at,
        amount_paise=p.amount_paise, segment=p.issuer, method=p.method,
        detail={"error_code": p.error_code, "error_source": p.error_source,
                "error_step": p.error_step, "error_reason": p.error_reason,
                "customer_ref": p.customer_ref, "order_id": p.order_id,
                "status": p.status},
    ) for p in latest.values()]


def linked_order_ids(links: list[dict], env: dict | None = None) -> set:
    """Resolve backing orders once per feed; never change account credentials."""
    out = set()
    for link in links:
        oid = link.get("order_id")
        if not oid and link.get("id"):
            # Fail closed: without the mapping we cannot exclude our own
            # recovery output or prove an order isn't a duplicate obligation.
            oid = _call("GET", f"/payment_links/{link['id']}", None,
                        env if env is not None else load_env()).get("order_id")
        if oid:
            out.add(oid)
    return out


def abandoned_checkouts(orders: list[dict], links: list[dict],
                        payments: list[RealPayment],
                        stale_after_minutes: int = 30,
                        now: datetime | None = None,
                        invoices: list[dict] | None = None,
                        env: dict | None = None,
                        linked_orders: set | None = None
                        ) -> list[RevenueAtRisk]:
    """Orders and links created, never paid, and old enough to be abandoned.

    Nothing FAILED here, which is the point: a failure-driven recovery system is
    structurally blind to this money. An order sitting unpaid emits no error
    code and appears in no failure report, yet it is revenue at risk in exactly
    the sense the brief means.

    `stale_after_minutes` is the grace period. Too short and we chase customers
    who are still typing their card number; too long and the intent is cold.
    """
    now = now or datetime.now(IST)
    cutoff = now - timedelta(minutes=stale_after_minutes)
    paid_orders = {p.order_id for p in payments if (p.succeeded or p.failed or p.status == "refunded") and p.order_id}

    # A payment link creates its own backing order, so counting both would
    # double the money at risk. The link is the customer-facing artefact and the
    # one we can act on, so it wins and its order is suppressed.
    #
    # The LIST response omits `order_id` -- it appears only when a link is
    # fetched individually. Without this the two never match and every link is
    # counted twice.
    # AN INVOICE ALSO CREATES A BACKING ORDER, and that order is unpaid by
    # definition while the invoice is unpaid. Without this, every overdue
    # receivable is counted twice -- once as an invoice and once as an
    # abandoned checkout -- and the book inflates by the whole receivables
    # balance. It did: Rs 2,96,050 across 16 orders.
    #
    # The suppression covers CANCELLED invoices too. Their orders are still
    # sitting there unpaid, but a merchant who cancelled an invoice has said
    # they no longer want the money, and chasing the ghost of it would be
    # worse than missing it.
    link_orders = {i.get("order_id") for i in (invoices or [])
                   if i.get("order_id")}
    link_orders.update(linked_order_ids(links, env) if linked_orders is None else linked_orders)

    out: list[RevenueAtRisk] = []
    for o in orders:
        if o.get("id") in link_orders:
            continue
        created = datetime.fromtimestamp(o.get("created_at", 0), IST)
        if (o.get("status") == "created" and o.get("id") not in paid_orders
                and created < cutoff and int(o.get("amount_paid") or 0) == 0):
            out.append(RevenueAtRisk(
                kind="checkout_abandoned", reference=o.get("id", ""),
                created_at=created, amount_paise=int(o.get("amount") or 0),
                segment="UNATTRIBUTED", method=None,
                detail={"source": "order", "attempts": o.get("attempts", 0),
                        "receipt": o.get("receipt")}))

    for l in links:
        # NEVER treat our own recovery link as revenue at risk.
        #
        # A recovery link is an unpaid payment link, which is exactly what an
        # abandoned checkout looks like from here. Left alone, the sequencer
        # opens a campaign against its own output: link begets campaign begets
        # link, compounding every pass, all of it counted as fresh revenue at
        # risk. The grace period hid it -- a link is only stale 30 minutes
        # after we create it, which is after any single run has finished.
        if (l.get("notes") or {}).get("source") == RECOVERY_TAG:
            continue
        created = datetime.fromtimestamp(l.get("created_at", 0), IST)
        if (l.get("status") in ("created", "partially_paid")
                and created < cutoff and int(l.get("amount_paid") or 0) == 0):
            out.append(RevenueAtRisk(
                kind="checkout_abandoned", reference=l.get("id", ""),
                created_at=created, amount_paise=int(l.get("amount") or 0),
                segment="UNATTRIBUTED", method=None,
                detail={"source": "payment_link", "short_url": l.get("short_url"),
                        "customer_ref": (l.get("customer") or {}).get("email") or (l.get("customer") or {}).get("contact"),
                        "status": l.get("status"),
                        "reminders": (l.get("reminders") or {}).get("status")}))
    return out


def fetch_invoices(limit: int = 200, env: dict | None = None) -> list[dict]:
    return _paged("/invoices", env or load_env(), limit)


def _invoice_customer(inv: dict) -> str | None:
    d = inv.get("customer_details") or inv.get("customer") or {}
    return (d.get("email") or d.get("customer_email")
            or d.get("contact") or d.get("customer_contact"))


def overdue_receivables(invoices: list[dict], now: datetime | None = None,
                        payment_terms_days: int = 14,
                        grace_days: int = 2) -> list[RevenueAtRisk]:
    """Invoices the customer agreed to pay and has not.

    THE DUE DATE IS DERIVED, not read. Razorpay carries `expire_by` -- when the
    link stops working -- which is a technical expiry and not a commercial one,
    and it is frequently unset. A receivable is overdue when the ISSUE date plus
    the agreed terms has passed, which is how every accounts-receivable ledger
    in the world works, so that is what is computed here.

    `grace_days` exists because invoices are routinely paid a day or two late by
    people who fully intend to pay. Chasing on the morning of day one is how a
    merchant annoys a paying customer.
    """
    now = now or datetime.now(IST)
    out: list[RevenueAtRisk] = []
    for inv in invoices:
        if (inv.get("notes") or {}).get("source") == RECOVERY_TAG:
            continue                       # never chase something we issued
        if inv.get("status") not in ("issued", "partially_paid", "expired"):
            continue                       # paid, cancelled or still a draft
        if int(inv.get("amount_paid") or 0) >= int(inv.get("amount") or 0):
            continue
        issued = inv.get("date") or inv.get("issued_at") or inv.get("created_at")
        if not issued:
            continue
        issued_at = datetime.fromtimestamp(int(issued), IST)
        due_at = issued_at + timedelta(days=payment_terms_days)
        if now < due_at + timedelta(days=grace_days):
            continue
        outstanding = int(inv.get("amount") or 0) - int(inv.get("amount_paid") or 0)
        out.append(RevenueAtRisk(
            kind="overdue_receivable", reference=inv.get("id", ""),
            created_at=issued_at, amount_paise=outstanding,
            segment="UNATTRIBUTED", method=None,
            detail={"source": "invoice",
                    # The LIST response nests the customer under
                    # `customer_details`, not `customer` -- the create request
                    # and the read response disagree, and reading the wrong one
                    # silently loses every opt-out check on this stream.
                    "customer_ref": _invoice_customer(inv),
                    "customer_name": (inv.get("customer_details") or {}).get("name"),
                    "due_at": due_at.isoformat(),
                    "days_overdue": (now - due_at).days,
                    "invoice_number": inv.get("invoice_number"),
                    "short_url": inv.get("short_url"),
                    "status": inv.get("status")}))
    return out


# ------------------------------------------------- normalise to window counts

def to_windows(payments: list[RealPayment], window_seconds: int = WINDOW_SECONDS
               ) -> dict[tuple[str, str], dict[int, dict]]:
    """Bucket real payments into the same 5-minute cells the detector expects.

    Key is (segment, method); value maps window index -> counts. This is the
    seam that lets the identical CUSUM run on real traffic: the detector never
    learns whether its input came from the simulator or the API.
    """
    cells: dict[tuple[str, str], dict[int, dict]] = {}
    for p in payments:
        key = (p.issuer, p.method or "unknown")
        w = int(p.created_at.timestamp() // window_seconds)
        cell = cells.setdefault(key, {})
        slot = cell.setdefault(w, {"n_attempts": 0, "failures_by_source": {},
                                   "failures_by_step": {},
                                   "amount_at_risk_paise": 0})
        slot["n_attempts"] += 1
        if p.failed:
            src = p.error_source or "unknown"
            step = p.error_step or "unknown"
            slot["failures_by_source"][src] = slot["failures_by_source"].get(src, 0) + 1
            slot["failures_by_step"][step] = slot["failures_by_step"].get(step, 0) + 1
            slot["amount_at_risk_paise"] += p.amount_paise
    return cells


def account_summary(env: dict | None = None) -> dict:
    """What the account actually holds. Used by the runner to report honestly
    on how much REAL data the batch was measured over.
    """
    env = env or load_env()
    payments = fetch_payments(500, env)
    orders = fetch_orders(500, env)
    links = fetch_payment_links(200, env)
    failures = payment_failures(payments)
    abandoned = abandoned_checkouts(orders, links, payments, env=env)
    return {
        "payments": len(payments),
        "succeeded": sum(1 for p in payments if p.succeeded),
        "failed": len(failures),
        "orders": len(orders),
        "payment_links": len(links),
        "at_risk_payment_failure_paise": sum(r.amount_paise for r in failures),
        "at_risk_abandoned_paise": sum(r.amount_paise for r in abandoned),
        "n_abandoned": len(abandoned),
        "segments": sorted({p.issuer for p in payments}),
    }
