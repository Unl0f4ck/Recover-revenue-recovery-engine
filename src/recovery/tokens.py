"""Signed customer links: opt-out, and instrument update.

getpaidhq ships "payment-update tokens so customers can fix their own details",
and recoup sends "signed card-update and opt-out links" on every message. Both
solve the same problem: some things only the customer can do, and the system
needs to hand them a way to do it that cannot be forged or shared.

TWO USES, ONE MECHANISM:

  OPT-OUT      the control the customer holds over this system. Until now the
               only way onto the suppression list was an operator running a CLI
               command, which means it depended on someone reading a reply and
               acting on it.

  INSTRUMENT   the correct terminal escalation for a HARD decline. A card
               UPDATE       reported stolen will not start working; retrying is
               pointless by definition. But the customer can still pay with
               something else, and that -- not write-off -- is where a hard
               decline should go.

WHY SIGNED. The token carries the customer reference in the URL, so an unsigned
one would let anyone opt out anyone else by editing an address, or enumerate a
merchant's customer list by walking ids. HMAC-SHA256 over the payload with a
server-side secret makes both impossible without holding the secret.

WHAT THIS IS NOT. It is not a session, and it grants nothing beyond the single
action it names. An instrument-update token cannot be replayed as an opt-out
token: the purpose is inside the signed payload, so changing it invalidates the
signature.

The HTTP endpoints these are consumed by are not part of this repo -- there is
no public server here. The token issuing, verification and expiry are, and they
are what the endpoint would be a thin shell around.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

OPT_OUT = "opt_out"
UPDATE_INSTRUMENT = "update_instrument"

DEFAULT_TTL = timedelta(days=30)


class BadToken(RuntimeError):
    pass


@dataclass(frozen=True)
class Token:
    purpose: str
    customer_ref: str
    reference: str | None
    issued_at: datetime
    expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(purpose: str, customer_ref: str, secret: str, now: datetime,
          reference: str | None = None,
          ttl: timedelta = DEFAULT_TTL) -> str:
    """Mint a token for exactly one action, for exactly one customer.

    `purpose` is inside the signed payload, not alongside it, so an
    instrument-update link cannot be edited into an opt-out link.
    """
    if purpose not in (OPT_OUT, UPDATE_INSTRUMENT):
        raise ValueError(f"unknown token purpose {purpose!r}")
    if not customer_ref:
        raise ValueError("a token needs a customer reference")
    if not secret:
        raise ValueError("refusing to sign a token with an empty secret")
    payload = {"p": purpose, "c": customer_ref, "r": reference,
               "i": int(now.timestamp()), "e": int((now + ttl).timestamp())}
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify(token: str, secret: str, now: datetime) -> Token:
    """Constant-time verification, then expiry. In that order.

    Checking expiry first would answer "is this a real token" through a
    different error message for an expired-but-valid one than for a forged one,
    which is a slow way of confirming a guess.
    """
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise BadToken("malformed token") from None
    expected = _b64(hmac.new(secret.encode(), body.encode(),
                             hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise BadToken("bad signature")
    try:
        d = json.loads(_unb64(body).decode())
    except Exception:                                    # noqa: BLE001
        raise BadToken("unreadable payload") from None

    t = Token(purpose=d["p"], customer_ref=d["c"], reference=d.get("r"),
              issued_at=datetime.fromtimestamp(d["i"], timezone.utc),
              expires_at=datetime.fromtimestamp(d["e"], timezone.utc))
    if t.expired(now):
        raise BadToken(f"token expired at {t.expires_at.isoformat()}")
    return t


def link(base_url: str, purpose: str, customer_ref: str, secret: str,
         now: datetime, reference: str | None = None,
         ttl: timedelta = DEFAULT_TTL) -> str:
    path = "opt-out" if purpose == OPT_OUT else "update-instrument"
    return (f"{base_url.rstrip('/')}/{path}"
            f"?t={issue(purpose, customer_ref, secret, now, reference, ttl)}")


def redeem_opt_out(token: str, secret: str, now: datetime,
                   path=None) -> str:
    """The customer clicked the opt-out link. Suppress them, permanently.

    This is what makes the suppression list reachable by the person it is for,
    rather than only by an operator who happened to read a reply.
    """
    from . import suppression
    t = verify(token, secret, now)
    if t.purpose != OPT_OUT:
        raise BadToken(f"token is for {t.purpose}, not opt-out")
    suppression.suppress(t.customer_ref, now,
                         reason="clicked the opt-out link",
                         action=suppression.OPT_OUT, source="customer",
                         path=path)
    return t.customer_ref
