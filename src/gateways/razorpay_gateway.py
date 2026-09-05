"""Razorpay adapter.

Wraps the existing `src/execution/razorpay.py` calls rather than replacing
them: that module already carries the hard-won knowledge about what Payment
Links can and cannot do, and rewriting it to fit an interface would risk losing
it. This adds the contract, the capability declaration, and webhook
verification.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from ..execution import razorpay as rz
from .base import Capabilities, RecoveryArtefact, WebhookEvent

IST = timezone(timedelta(hours=5, minutes=30))

SIGNATURE_HEADER = "x-razorpay-signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

# Razorpay event -> what it means to a recovery sequencer. Anything not here is
# normalised to "other" and recorded rather than dropped: medusa#16398 is
# precisely the bug where a handler silently returns early on an event it does
# not recognise, and the order sits pending forever.
EVENT_KINDS = {
    "payment_link.paid": "recovery_paid",
    "payment_link.partially_paid": "recovery_partially_paid",
    "payment_link.expired": "recovery_expired",
    "payment_link.cancelled": "recovery_cancelled",
    "payment.failed": "payment_failed",
    "payment.captured": "payment_captured",
    "payment.authorized": "payment_authorized",
    "order.paid": "order_paid",
    "refund.created": "refunded",
}


class BadSignature(RuntimeError):
    """The event did not come from Razorpay, or came mangled."""


class RazorpayGateway:
    def __init__(self, env: dict | None = None):
        self.env = env or rz.load_env()

    # ------------------------------------------------------------------ caps
    def capabilities(self) -> Capabilities:
        """What Razorpay Payment Links can and cannot do for us.

        `can_restrict_methods=False` is the Day-4 finding: the create API
        exposes no field for limiting payment methods, and passing
        `options.checkout.method` is accepted and silently ignored -- the worst
        kind of failure, because it looks like it worked.

        `can_charge_saved_instrument=False` is an ACCOUNT fact, not an API one.
        Razorpay supports tokens, e-mandate and UPI Autopay; this account holds
        none of them, so a silent re-present is impossible here. Stated as a
        capability so the sequencer reports the step unexecutable rather than
        modelling it.
        """
        return Capabilities(
            name="razorpay",
            can_restrict_methods=False,
            can_notify=True,                  # links can SMS/email natively
            can_charge_saved_instrument=False,
            webhook_signature="hmac-sha256-rawbody",
            # VERIFIED BY THE API, which said so itself on reaching it:
            #
            #   429 {"code": "RATE_LIMIT_EXCEEDED",
            #        "description": "test mode limit of 30 reached for
            #                        payment_link"}
            #
            # Worth recording how nearly this was lost. The number was written
            # here as an assertion, then briefly "corrected" to unverified on
            # the grounds that nothing had enforced it -- at a moment when the
            # account was at 30 and every further attempt was failing for
            # exactly this reason. The failures were being read as ordinary
            # rate limiting because they share the 429 status; only the
            # `description` distinguishes them, and nothing was reading it.
            #
            # TWO DIFFERENT 429s, and they need different handling. "Too many
            # requests" is transient and the backoff in `_call` is right for
            # it. "test mode limit of 30 reached" is permanent until links are
            # deleted, and retrying it five times with exponential sleep just
            # wastes half a minute before failing anyway.
            max_recovery_artefacts=30,
            notes="method restriction unavailable on Payment Links; no saved "
                  "token or mandate on this account",
        )

    # ------------------------------------------------------------- artefacts
    def create_recovery(self, amount_paise: int, description: str,
                        exclude_methods: list[str] | None = None,
                        notify: dict | None = None,
                        customer: dict | None = None,
                        notes: dict | None = None,
                        dry_run: bool = False) -> RecoveryArtefact:
        exclude = list(exclude_methods or [])
        notify = notify or {"sms": False, "email": False}
        if dry_run:
            return RecoveryArtefact(
                gateway="razorpay", reference=None, url=None,
                amount_paise=amount_paise, mode="SIMULATED",
                notified={k: False for k in notify},
                detail=f"dry run: would create a link excluding "
                       f"{exclude or 'nothing'}")
        r = rz.create_recovery_link(amount_paise, exclude, description,
                                    self.env, notify=notify,
                                    customer=customer, notes=notes)
        return RecoveryArtefact(
            gateway="razorpay", reference=r.reference, url=r.url,
            amount_paise=amount_paise, mode=r.mode,
            method_restriction_enforced=r.rail_restriction_enforced,
            notified=dict(notify), detail=r.detail)

    def fetch_recovery(self, reference: str) -> RecoveryArtefact:
        info = rz.fetch_payment_link(reference, self.env)
        return RecoveryArtefact(
            gateway="razorpay", reference=info.get("id"),
            url=info.get("short_url"),
            amount_paise=int(info.get("amount") or 0),
            mode="REAL", status=info.get("status", "unknown"),
            amount_paid_paise=int(info.get("amount_paid") or 0),
            detail=f"status={info.get('status')}")

    def cancel_recovery(self, reference: str):
        return rz._call("POST", f"/payment_links/{reference}/cancel", None, self.env)

    def charge_mandate(self, amount_paise: int, reference: str,
                       attempt_no: int = 0) -> RecoveryArtefact:
        """Not on this account.

        Razorpay the product supports tokens, e-mandate and UPI Autopay. This
        ACCOUNT holds none of them, and `/subscriptions` and `/plans` both
        return 401 while `/payments` and `/customers` succeed -- feature
        gating, not a credentials problem. Raising here is correct: the
        capability already says false, so the sequencer never calls this, and
        anything that does has skipped the check.
        """
        raise NotImplementedError(
            "no saved token or e-mandate on this Razorpay account; "
            "capabilities().can_charge_saved_instrument is False")

    # -------------------------------------------------------------- webhooks
    def verify_webhook(self, raw_body: bytes, headers: dict, secret: str,
                       now: datetime | None = None,
                       previous_secrets: list[str] | None = None
                       ) -> WebhookEvent:
        """HMAC-SHA256 over the EXACT RAW BYTES, compared in constant time.

        Three things this gets right that are easy to get wrong:

        RAW BYTES, never a re-serialisation. `json.dumps(json.loads(body))` is
        not the same string -- key order, whitespace and unicode escaping all
        differ -- so a handler that parses before verifying rejects every
        genuine event and, worse, invites someone to "fix" it by loosening the
        check.

        CONSTANT-TIME COMPARISON. `==` on a signature leaks its prefix through
        timing. `hmac.compare_digest` is the whole mitigation and costs nothing.

        SECRET ROTATION. Razorpay retries failed deliveries, and a retry may
        arrive after the secret was rotated, signed with the OLD one. Accepting
        `previous_secrets` is what stops a rotation silently dropping a day of
        events.

        NOTE ON TIMESTAMPS. Razorpay does NOT sign a timestamp -- unlike Stripe,
        whose scheme is `t=...,v1=...` with a tolerance window. Copying Stripe's
        pattern here would mean inventing a check with nothing to check against.
        Replay protection therefore comes from event-id deduplication upstream
        (`x-razorpay-event-id`), not from the signature, and that difference is
        recorded rather than papered over.
        """
        lower = {str(k).lower(): v for k, v in (headers or {}).items()}
        got = lower.get(SIGNATURE_HEADER)
        if not got:
            raise BadSignature(f"missing {SIGNATURE_HEADER} header")
        if not isinstance(raw_body, (bytes, bytearray)):
            raise BadSignature("raw_body must be bytes; a parsed and "
                               "re-serialised body will never match")

        for candidate in [secret, *(previous_secrets or [])]:
            if not candidate:
                continue
            expected = hmac.new(candidate.encode(), raw_body,
                                hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, str(got)):
                break
        else:
            raise BadSignature("signature does not match any known secret")

        import json
        payload = json.loads(raw_body.decode("utf-8"))
        event = payload.get("event", "")
        return WebhookEvent(
            gateway="razorpay",
            event_id=str(lower.get(EVENT_ID_HEADER)
                         or payload.get("id") or ""),
            event=event,
            kind=EVENT_KINDS.get(event, "other"),
            at=datetime.fromtimestamp(payload.get("created_at", 0), IST),
            reference=_reference_of(payload),
            amount_paise=_amount_of(payload),
            payload=payload,
        )


def _entity(payload: dict, name: str) -> dict:
    return ((payload.get("payload") or {}).get(name) or {}).get("entity") or {}


def _reference_of(payload: dict) -> str | None:
    if payload.get("event") == "order.paid":
        return _entity(payload, "order").get("id") or _entity(payload, "payment").get("order_id")
    for name in ("payment_link", "payment", "order", "refund"):
        e = _entity(payload, name)
        if e.get("id"):
            return e["id"]
    return None


def _amount_of(payload: dict) -> int:
    for name in ("payment_link", "payment", "order"):
        e = _entity(payload, name)
        if e:
            return int(e.get("amount_paid") or e.get("amount") or 0)
    return 0
