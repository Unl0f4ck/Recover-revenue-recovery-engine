"""The notification ledger. Sent is not delivered, and neither is recovered.

For most of this project nothing was ever sent to anyone: every recovery link
was created with `notify: {sms: false, email: false}`. That was honest, and it
meant "contact made" really meant "link created" -- the compliance machinery
guarding a door nobody walked through.

Turning delivery on requires somewhere to record what happened to each message,
which is what Etherlabs (a notifications ledger with duplicate suppression) and
emp-billing (`notifications` and `webhook_deliveries`, both with delivery
status) both build. Without it there is exactly one bit of information -- we
called the API -- standing in for a chain of quite different facts:

    REQUESTED   we asked the provider to send it
    SENT        the provider accepted it
    DELIVERED   it reached the handset or inbox
    FAILED      it bounced, or the number is dead
    SUPPRESSED  we declined to send it

A system that cannot tell those apart cannot explain a bad campaign, and will
report a bounce rate of zero forever.

WHAT WE CAN ACTUALLY OBSERVE. Razorpay accepts `notify: {sms, email}` on a
Payment Link and sends the message itself. It does not report per-message
delivery back to us. So on this provider a notification legitimately stops at
REQUESTED, and this module says so rather than promoting it to SENT because the
API call returned 200. The states above the line exist because a second channel
(a real ESP with delivery webhooks) is the obvious next adapter, and inventing
the vocabulary later would mean rewriting history.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .jsonl import index_by, read_jsonl

NOTIFICATIONS = Path("data/live/notifications.jsonl")

REQUESTED = "requested"          # we asked the provider to send
SENT = "sent"                    # the provider accepted it
DELIVERED = "delivered"          # it reached the recipient
FAILED = "failed"                # bounced / undeliverable
SUPPRESSED = "suppressed"        # we chose not to send

TERMINAL = {DELIVERED, FAILED, SUPPRESSED}


@dataclass(frozen=True)
class Notification:
    at: datetime
    reference: str               # the at-risk object this concerns
    sequence_id: str
    attempt_no: int
    channel: str                 # sms | email | none
    status: str
    customer_ref: str | None = None
    gateway: str = "razorpay"
    artefact: str | None = None  # provider id of the link
    detail: str = ""
    extra: dict = field(default_factory=dict)


def read(path: Path | None = None) -> list[dict]:
    p = path or NOTIFICATIONS
    return read_jsonl(p)


def append(n: Notification, path: Path | None = None) -> None:
    p = path or NOTIFICATIONS
    p.parent.mkdir(parents=True, exist_ok=True)
    d = asdict(n)
    d["at"] = n.at.isoformat()
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(d) + "\n")


def record(reference: str, sequence_id: str, attempt_no: int, channel: str,
           status: str, at: datetime, customer_ref: str | None = None,
           artefact: str | None = None, detail: str = "",
           path: Path | None = None, **extra) -> None:
    append(Notification(at=at, reference=reference, sequence_id=sequence_id,
                        attempt_no=attempt_no, channel=channel, status=status,
                        customer_ref=customer_ref, artefact=artefact,
                        detail=detail, extra=extra), path)


def for_reference(reference: str, path: Path | None = None) -> list[dict]:
    return index_by(path or NOTIFICATIONS, "reference").get(reference, [])


def latest_status(reference: str, attempt_no: int, channel: str,
                  path: Path | None = None) -> str | None:
    """The most recent state of one message. Append-only, so last wins."""
    last = None
    for n in for_reference(reference, path):
        if n.get("attempt_no") == attempt_no and n.get("channel") == channel:
            last = n.get("status")
    return last


def already_sent(reference: str, attempt_no: int, channel: str,
                 path: Path | None = None, since: datetime | None = None
                 ) -> bool:
    """Duplicate suppression, as Etherlabs does it -- but TIME-BOUNDED.

    A retried pass, a replayed webhook or a cron overlapping a manual run must
    not send the same attempt's message twice. The idempotency key stops the
    provider double-charging; this stops the customer being messaged twice for
    one rung of the ladder.

    WHY `since` EXISTS. This check is only ever load-bearing in one window: we
    recorded that a message was requested, and then died before recording the
    outcome. Once an outcome IS recorded the attempt counter advances and the
    check cannot fire anyway. So an unbounded version protects nothing extra
    and stalls a sequence forever after a single crash -- the message shows as
    requested, no outcome ever arrives, and every future pass skips the rung.

    Bounded to the minimum gap between attempts, it does what it is for
    (blocking a rapid double-fire) and lets a stalled attempt be re-issued once
    enough time has passed that a duplicate is the lesser risk.
    """
    if since is None:
        return latest_status(reference, attempt_no, channel, path) in (
            REQUESTED, SENT, DELIVERED)
    for n in for_reference(reference, path):
        if n.get("attempt_no") != attempt_no or n.get("channel") != channel:
            continue
        if n.get("status") not in (REQUESTED, SENT, DELIVERED):
            continue
        if datetime.fromisoformat(n["at"]) >= since:
            return True
    return False


def recent_contacts(customer_ref: str | None, since: datetime,
                    path: Path | None = None) -> int:
    """How many times we have reached at this person since `since`.

    This is what `max_contacts_per_customer_per_day` needs and never had: the
    per-reference ceiling counts messages about ONE order, and a customer with
    six abandoned checkouts could legitimately clear it six times over.
    """
    if not customer_ref:
        return 0
    from .suppression import normalise
    key = normalise(customer_ref)
    # DISTINCT MESSAGES, not rows. One message accumulates several status rows
    # as it moves requested -> sent -> delivered, and this ledger is
    # append-only, so counting rows counts a single SMS two or three times and
    # a per-customer cap of two would block after the first message.
    seen: set[tuple] = set()
    for row in read(path):
        if normalise(row.get("customer_ref")) != key:
            continue
        if row.get("status") not in (REQUESTED, SENT, DELIVERED):
            continue
        if datetime.fromisoformat(row["at"]) < since:
            continue
        seen.add((row.get("reference"), row.get("attempt_no"),
                  row.get("channel")))
    return len(seen)


def within_daily_cap(customer_ref: str | None, now: datetime, cap: int,
                     path: Path | None = None) -> tuple[bool, str]:
    """The per-customer daily ceiling, finally wired.

    `max_contacts_per_customer_per_day` has been in declines.yaml since the
    schedules were written and was read by nothing -- there was no per-customer
    contact history to count. There is now.
    """
    if not customer_ref:
        return True, "no customer identity; per-customer cap not applicable"
    used = recent_contacts(customer_ref, now - timedelta(days=1), path)
    if used >= cap:
        return False, (f"customer contacted {used} time(s) in the last 24h, "
                       f"cap is {cap}")
    return True, f"customer contact {used + 1}/{cap} today"


def summary(path: Path | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in read(path):
        out[n["status"]] = out.get(n["status"], 0) + 1
    return out
