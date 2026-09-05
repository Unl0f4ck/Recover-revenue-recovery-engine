"""The live loop: reconcile, verify obligations, diagnose, then advance."""
from dataclasses import replace
from datetime import datetime

from ..execution import razorpay as rz
from ..ingest import razorpay_source as source
from . import campaign as C, ledger as L, dunning as D

UNPAID_STATES = {"failed", "created", "attempted", "pending", "halted", "issued"}


class ReplacementGateway:
    """Retire old links only at the already-authorized create boundary.

    Planning, a contact hold, or an exhausted batch budget must not invalidate
    the link the customer is currently using.
    """
    def __init__(self, gateway, path, now, cfg, env):
        self.gateway, self.path, self.now, self.cfg, self.env = gateway, path, now, cfg, env

    def __getattr__(self, key):
        return getattr(self.gateway, key)

    def create_recovery(self, *args, **kwargs):
        ref = (kwargs.get("notes") or {}).get("reference")
        for link in {e.get("execution_reference") for e in L.events_for(ref, self.path)
                     if str(e.get("execution_reference") or "").startswith("plink_")}:
            art = self.gateway.fetch_recovery(link)
            if art.paid:
                C.reconcile_links(self.now, self.env, self.path, self.gateway, self.cfg)
                raise RuntimeError("previous link was paid; replacement refused")
            if art.status not in ("cancelled", "expired"):
                if art.status != "created" or art.amount_paid_paise:
                    raise RuntimeError("previous link state uncertain or partially paid; replacement held")
                self.gateway.cancel_recovery(link)
                if self.gateway.fetch_recovery(link).status != "cancelled":
                    raise RuntimeError("previous link cancellation is unconfirmed; replacement held")
        return self.gateway.create_recovery(*args, **kwargs)


def observe(reference, env, order_id=None, expected_amount=None):
    """Read a case directly; list pagination is never evidence of settlement."""
    target = order_id or reference
    prefix = target.split("_", 1)[0]
    endpoint = {"pay": "payments", "order": "orders", "inv": "invoices",
                "plink": "payment_links", "sub": "subscriptions"}.get(prefix)
    if not endpoint:
        raise ValueError("case has no supported gateway identity")
    row = rz._call("GET", f"/{endpoint}/{target}", None, env)
    if expected_amount is not None and row.get("status") in UNPAID_STATES:
        # A partially settled or edited invoice must not receive a new link for
        # its stale original balance. Hold for an operator, don't silently
        # rewrite the amount promised by the existing campaign.
        if "amount" not in row or row.get("currency", "INR") != "INR":
            raise ValueError("unverified amount or unsupported currency")
        remaining = int(row.get("amount_due") if row.get("amount_due") is not None
                        else int(row["amount"]) - int(row.get("amount_paid") or 0))
        if remaining != expected_amount:
            raise ValueError("outstanding amount changed; review before recovery")
    return row.get("status", "unknown")


def diagnose_items(items, payments):
    from .degradation import scan
    from dataclasses import asdict
    result = scan(payments)
    if result.incident and result.diagnosis and result.diagnosis.confidence >= 0.75:
        affected = {(c.segment, c.method) for c in result.alerting_cells}
        items = [replace(i, detail={**i.detail,
                    "diagnosis_family": result.diagnosis.mechanism_family,
                    "diagnosis": {"cause": result.diagnosis.cause_node,
                                  "confidence": result.diagnosis.confidence,
                                  "action": result.action.value}})
                 if (i.segment, i.method) in affected else i for i in items]
    return items, result


def live_pass(items, now, cfg, *, path, gateway=None, env=None, **kwargs):
    """Caller holds the same exclusive lock as webhook ingestion.

    Fail closed per case when its status cannot be verified. API failures do
    not erase campaigns or turn absence from a feed into an unpaid verdict.
    """
    env = env or rz.load_env()
    from ..policy import load_policy
    policy = kwargs.get("policy") if kwargs.get("policy") is not None else load_policy()
    killed = kwargs.get("kill_switch")
    if killed is None:
        killed = bool(policy.get("bounds", {}).get("global_kill_switch"))
    if killed:
        return C.CampaignResult(now, halted="global_kill_switch is set; no gateway writes")
    C.reconcile_links(now, env, path, gateway, cfg)
    states, blocked = {}, set()
    rows = {i.reference: i.detail for i in items}
    amounts = {i.reference: i.amount_paise for i in items}
    for e in L.read(path):
        if e["event"] == L.SEQUENCE_OPENED and not L.is_stopped(e["reference"], path):
            rows[e["reference"]] = e.get("extra") or {}
            amounts[e["reference"]] = e["amount_paise"]
    for ref, detail in rows.items():
        try:
            states[ref] = observe(ref, env, detail.get("order_id"), expected_amount=amounts[ref])
            if states[ref] not in UNPAID_STATES | set(cfg["stopping"]["terminal_payment_states"]):
                blocked.add(ref)
        except Exception:
            blocked.add(ref)
    from ..gateways import get_gateway
    gw = gateway or get_gateway("razorpay", env=env)
    guarded = ReplacementGateway(gw, path, now, cfg, env)
    result = C.run_pass([i for i in items if i.reference not in blocked], now, cfg,
                        dry_run=False, env=env, states=states, path=path,
                        gateway=guarded, blocked_refs=blocked, **kwargs)
    # A settlement webhook may already have stopped the sequence, excluding it
    # from the next feed/open-sequence sweep. Still retire its payable links.
    all_refs = {e["reference"] for e in L.read(path) if e["event"] == L.SEQUENCE_OPENED}
    for ref in all_refs:
        seq = C.rebuild(ref, cfg, path)
        if seq is None:
            continue
        closed_financially = any(e["event"] == L.SEQUENCE_STOPPED and
                                (e.get("extra") or {}).get("stop_reason") in (D.STOP_RECOVERED, D.STOP_TERMINAL_STATE)
                                for e in L.events_for(ref, path))
        if not closed_financially:
            continue
        for link in {e.get("execution_reference") for e in L.events_for(ref, path)
                     if str(e.get("execution_reference") or "").startswith("plink_")}:
            try:
                art = gw.fetch_recovery(link)
                if art.paid:
                    C.reconcile_links(now, env, path, gw, cfg)
                elif art.status not in ("cancelled", "expired"):
                    gw.cancel_recovery(link)
                    confirmed = gw.fetch_recovery(link)
                    if confirmed.status != "cancelled":
                        blocked.add(ref)
            except Exception:
                blocked.add(ref)
    for ref in blocked:
        result.deferred.append({"reference": ref, "until": None,
                               "reason": "gateway state unavailable; held for reconciliation"})
    return result
