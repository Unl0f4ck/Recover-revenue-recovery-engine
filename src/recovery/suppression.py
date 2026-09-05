"""Opt-out. The one control a customer holds over this system.

Every recovery system in docs/PRIOR_ART.md that contacts customers ships one --
recoup sends "signed card-update and opt-out links" on every message. We had
none. A system that messages people three times per unit of at-risk revenue,
across an unbounded number of units, with no way for a person to say stop, is
not compliant in any sense the track brief means, however carefully its quiet
hours are configured.

TWO PROPERTIES, both deliberate:

  IT IS ABSOLUTE. A suppressed contact is never messaged again -- not by a
  different campaign, not by a higher-value one, not by a different channel. The
  check sits in `authorize_contact`, above the quiet-hours and ceiling checks,
  because those are about WHEN to contact someone and this is about WHETHER.

  IT IS APPEND-ONLY, like the ledger. An opt-out is a fact, and the record of it
  outlives the campaign that prompted it. Un-suppressing is itself an event, so
  "who removed this person from the list, and when" is answerable.

WHAT WE CANNOT DO. Suppression is keyed on whatever identity the API gives us.
Razorpay payments carry `email` and `contact`; orders carry neither. So an
abandoned order has no contactable identity, which means it also has no
suppressible one -- and rather than let that silently pass the check, a
campaign with no customer reference records that the check could not be applied.
Silence there would be the worst outcome: a compliance control that reports
success because it had nothing to compare against.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .jsonl import read_jsonl

SUPPRESSION = Path("data/live/suppression.jsonl")

OPT_OUT = "opt_out"                  # the customer asked
BOUNCE = "hard_bounce"               # the channel says this address is dead
COMPLAINT = "spam_complaint"         # they reported us
OPERATOR = "operator"                # a human removed them
RESTORED = "restored"                # ...and a human put them back


@dataclass(frozen=True)
class SuppressionEvent:
    at: datetime
    customer_ref: str                # email or phone, normalised
    action: str                      # one of the constants above
    reason: str = ""
    source: str = ""                 # who or what recorded it


def normalise(ref: str | None) -> str | None:
    """One customer, one key.

    `Foo@Example.com ` and `foo@example.com` are the same person, and so are
    `+91 98765 43210` and `+919876543210`. Suppression that misses because of
    whitespace is suppression that does not exist.
    """
    if not ref:
        return None
    r = ref.strip().lower()
    if "@" in r:
        return r
    digits = "".join(c for c in r if c.isdigit())
    return ("+" + digits) if digits else None


def read(path: Path | None = None) -> list[dict]:
    p = path or SUPPRESSION
    return read_jsonl(p)


def record(customer_ref: str, action: str, at: datetime, reason: str = "",
           source: str = "", path: Path | None = None) -> None:
    key = normalise(customer_ref)
    if not key:
        raise ValueError(f"cannot suppress an empty customer reference: {customer_ref!r}")
    p = path or SUPPRESSION
    p.parent.mkdir(parents=True, exist_ok=True)
    e = SuppressionEvent(at=at, customer_ref=key, action=action, reason=reason,
                         source=source)
    d = asdict(e)
    d["at"] = at.isoformat()
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(d) + "\n")


def suppress(customer_ref: str, at: datetime, reason: str = "",
             action: str = OPT_OUT, source: str = "customer",
             path: Path | None = None) -> None:
    record(customer_ref, action, at, reason, source, path)


def restore(customer_ref: str, at: datetime, reason: str, source: str,
            path: Path | None = None) -> None:
    """Undo a suppression. Requires a reason and a named source, because
    putting someone back on a contact list is a decision someone should own."""
    if not reason or not source:
        raise ValueError("restoring a suppressed contact requires a reason "
                         "and a source")
    record(customer_ref, RESTORED, at, reason, source, path)


def is_suppressed(customer_ref: str | None, path: Path | None = None) -> bool:
    """Latest event wins. Anything that is not an explicit restore suppresses."""
    key = normalise(customer_ref)
    if not key:
        return False
    last = None
    for e in read(path):
        if e.get("customer_ref") == key:
            last = e.get("action")
    return last is not None and last != RESTORED


def suppressed_set(path: Path | None = None) -> set[str]:
    out: dict[str, str] = {}
    for e in read(path):
        out[e["customer_ref"]] = e["action"]
    return {k for k, v in out.items() if v != RESTORED}


def reason_for(customer_ref: str | None, path: Path | None = None) -> str:
    key = normalise(customer_ref)
    if not key:
        return ""
    last = None
    for e in read(path):
        if e.get("customer_ref") == key:
            last = e
    if not last or last.get("action") == RESTORED:
        return ""
    return f"{last['action']}" + (f" ({last['reason']})" if last.get("reason") else "")
