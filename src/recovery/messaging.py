"""Send an existing case's link once. No arbitrary recipients, no blind retries."""
from datetime import timedelta
import copy
import hashlib
import json
import re

from ..execution import razorpay as rz
from . import campaign as C, dunning as D, ledger as L, notify, channels, suppression
from .operations import Operations
from .service import observe


class SendBlocked(ValueError):
    pass


def send_link(reference, channel, now, merchant, env):
    """Caller holds the writer lock. A provider acknowledgement is not delivery."""
    if channel not in ("sms", "email"):
        raise SendBlocked("Choose SMS or email")
    seq = C.rebuild(reference, merchant.cfg, merchant.ledger)
    if seq is None:
        raise SendBlocked("Case not found in this account")
    if L.is_stopped(reference, merchant.ledger) or L.has_succeeded(reference, merchant.ledger):
        raise SendBlocked("Campaign is closed; no message sent")
    from ..policy import load_policy
    if D.check_stop(seq, now, cfg=merchant.cfg, path=merchant.ledger,
                    kill_switch=bool(load_policy().get("bounds", {}).get("global_kill_switch"))):
        raise SendBlocked("Campaign stopping rules block further messages")
    rows = L.events_for(reference, merchant.ledger)
    event = next((e for e in reversed(rows) if str(e.get("execution_reference", "")).startswith("plink_")), None)
    if event is None:
        raise SendBlocked("Create a recovery link for this case first")
    link, attempt = event["execution_reference"], event["attempt_no"]
    if not re.fullmatch(r"plink_[A-Za-z0-9]+", link):
        raise SendBlocked("Invalid payment link identity")
    # Any prior request is a hold, including uncertain writes. Clicking again
    # must never turn a lost provider response into a second customer message.
    previous = [n for n in notify.for_reference(reference, merchant.notifications)
                if n.get("attempt_no") == attempt and n.get("channel") == channel]
    if previous:
        return {"status": previous[-1]["status"], "duplicate": True,
                "detail": "Already requested or held; no additional message sent"}
    wf = copy.deepcopy(channels.load_workflows())
    wf.setdefault("delivery", {})["redirect"] = {"enabled": False}
    to, _ = C.contact_for(seq, wf)
    if not to or not C.delivery_for(seq, {channel: True}, wf).get(channel):
        raise SendBlocked(f"This case has no known {channel} recipient")
    step = D.Step(attempt, now, "payment_link", "same")
    decision = D.authorize_contact(seq, step, now, merchant.cfg, merchant.ledger,
                                   merchant.suppression, merchant.notifications, wf, merchant.promise)
    if not decision.allowed or not decision.executable:
        raise SendBlocked(decision.reason)
    recent = [n for n in notify.for_reference(reference, merchant.notifications)
              if n.get("channel") in ("email", "sms") and n.get("status") != notify.SUPPRESSED]
    if recent:
        from datetime import datetime
        last = max(datetime.fromisoformat(n["at"]) for n in recent)
        gap = timedelta(hours=merchant.cfg["compliance"].get("min_hours_between_attempts", 4))
        if now < last + gap:
            raise SendBlocked("Minimum gap between messages has not elapsed")
    try:
        state = observe(reference, env, seq.extra.get("order_id"), expected_amount=seq.at_risk_paise)
        art = rz._call("GET", f"/payment_links/{link}", None, env)
    except Exception:
        raise SendBlocked("Cannot verify current payment state; sending held") from None
    if state not in ("failed", "created", "attempted", "pending", "halted", "issued") or art.get("status") != "created":
        raise SendBlocked("Payment is settled, closed, or uncertain; no message sent")
    actual = (art.get("customer") or {}).get("email" if channel == "email" else "contact")
    if suppression.normalise(actual) != suppression.normalise(to):
        raise SendBlocked("Payment link recipient differs from this case; sending held")
    journal = Operations(merchant.notifications.with_suffix(".sqlite3"))
    key = f"{reference}:{attempt}:{channel}:{link}"
    fingerprint = hashlib.sha256(json.dumps({"to": to, "link": link, "channel": channel}, sort_keys=True).encode()).hexdigest()
    fresh, cached = journal.claim(key, fingerprint)
    if not fresh:
        return cached or {"status": "unknown", "duplicate": True, "detail": "Previous send is uncertain; no retry issued"}
    notify.record(reference, seq.sequence_id, attempt, channel, notify.REQUESTED, now,
                  customer_ref=seq.customer_ref, artefact=link, path=merchant.notifications,
                  detail="Explicit operator request to Razorpay")
    try:
        rz._call("POST", f"/payment_links/{link}/notify_by/{channel}", None, env)
        result = {"status": "requested", "detail": "Razorpay accepted the notification request; delivery is unconfirmed"}
    except Exception:
        result = {"status": "unknown", "detail": "Provider did not confirm the request. Check Razorpay before retrying; automatic resend is blocked"}
    notify.record(reference, seq.sequence_id, attempt, channel, result["status"], now,
                  customer_ref=seq.customer_ref, artefact=link, path=merchant.notifications,
                  detail=result["detail"])
    journal.finish(key, result)
    return result
