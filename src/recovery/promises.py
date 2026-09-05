"""Promise-to-pay. A debtor who names a date is not a debtor who says nothing.

Named in the track brief, and it is the one collections concept that changes
behaviour rather than just recording it. Someone who replies "I'll pay on
Friday" has done something meaningful: they have acknowledged the debt and
committed to a date. Chasing them on Wednesday anyway is the single most
reliable way to make them stop answering, and it converts a customer who was
going to pay into one who now resents you.

So a promise PAUSES the ladder. It does not cancel it, and it does not restart
it -- the sequence resumes from where it stood, on the promised date plus a
short grace.

THREE THINGS THIS GETS RIGHT, each of which is a way collections software
usually gets it wrong:

  A PROMISE HAS A CEILING. Without `max_horizon_days` a debtor parks a debt
  forever by promising an ever-later date, and every promise looks like
  progress in the report while nothing is collected.

  A BROKEN PROMISE IS INFORMATION. Resuming the ladder exactly where it paused
  treats a broken commitment as if nothing happened. It moves the case FORWARD
  instead -- the next contact is not the one they ignored, and after
  `max_broken` the case goes to a person.

  A KEPT PROMISE IS THE ONLY GOOD OUTCOME, and it is confirmed by the payment
  arriving, never by the date passing. `kept()` asks the ledger, not the clock.

Append-only, like every other record here. A promise is a fact about a
conversation, and amending it in place would lose the conversation.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .jsonl import index_by, read_jsonl

PROMISES = Path("data/live/promises.jsonl")

MADE = "made"
KEPT = "kept"
BROKEN = "broken"
CANCELLED = "cancelled"


class PromiseRefused(ValueError):
    """The promise was not accepted, and the reason is the message."""


@dataclass(frozen=True)
class PromiseEvent:
    at: datetime
    reference: str
    action: str
    pay_by: str | None = None            # ISO date the debtor named
    amount_paise: int = 0
    channel: str = ""                    # how they told us
    note: str = ""
    recorded_by: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Promise:
    reference: str
    pay_by: datetime
    amount_paise: int
    made_at: datetime
    state: str = MADE
    note: str = ""
    broken_count: int = 0

    def due(self, now: datetime, grace_days: int) -> bool:
        return now >= self.pay_by + timedelta(days=grace_days)

    def days_left(self, now: datetime) -> int:
        return (self.pay_by - now).days


def read(path: Path | None = None) -> list[dict]:
    return read_jsonl(path or PROMISES)


def _for(reference: str, path: Path | None = None) -> list[dict]:
    return index_by(path or PROMISES, "reference").get(reference, [])


def _append(e: PromiseEvent, path: Path | None = None) -> None:
    p = path or PROMISES
    p.parent.mkdir(parents=True, exist_ok=True)
    d = asdict(e)
    d["at"] = e.at.isoformat()
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(d) + "\n")


def record(reference: str, pay_by: datetime, now: datetime,
           amount_paise: int = 0, channel: str = "", note: str = "",
           recorded_by: str = "", cfg: dict | None = None,
           path: Path | None = None) -> Promise:
    """A debtor named a date. Accept it, within limits.

    Refused rather than silently clamped when the date is impossible or too far
    out: an operator who typed 2027 should be told, not quietly given 2026.
    """
    cfg = (cfg or {}).get("promise_to_pay", {}) if cfg else {}
    horizon = int(cfg.get("max_horizon_days", 30))
    max_broken = int(cfg.get("max_broken", 2))

    if pay_by <= now:
        raise PromiseRefused(
            f"a promise must name a FUTURE date; {pay_by:%d %b} has passed")
    if (pay_by - now).days > horizon:
        raise PromiseRefused(
            f"{pay_by:%d %b} is {(pay_by - now).days} days out; the limit is "
            f"{horizon}. Without a ceiling a debt can be parked forever by "
            f"promising an ever-later date.")

    broken = count_broken(reference, path)
    if broken >= max_broken:
        raise PromiseRefused(
            f"this debtor has broken {broken} promise(s); the limit is "
            f"{max_broken}. A person should agree the next one.")

    _append(PromiseEvent(at=now, reference=reference, action=MADE,
                         pay_by=pay_by.isoformat(), amount_paise=amount_paise,
                         channel=channel, note=note, recorded_by=recorded_by),
            path)
    return Promise(reference=reference, pay_by=pay_by,
                   amount_paise=amount_paise, made_at=now, note=note,
                   broken_count=broken)


def count_broken(reference: str, path: Path | None = None) -> int:
    return sum(1 for e in _for(reference, path) if e["action"] == BROKEN)


def active(reference: str, now: datetime, cfg: dict | None = None,
           path: Path | None = None) -> Promise | None:
    """The promise currently holding this case, if any.

    Latest event wins. A promise that has passed its date plus grace is no
    longer active -- it is either kept or broken, and which one is settled by
    `settle()` against the ledger, not by this function guessing.
    """
    grace = int(((cfg or {}).get("promise_to_pay") or {}).get("grace_days", 1))
    latest: dict | None = None
    for e in _for(reference, path):
        latest = e
    if not latest or latest["action"] != MADE:
        return None
    p = Promise(reference=reference,
                pay_by=datetime.fromisoformat(latest["pay_by"]),
                amount_paise=int(latest.get("amount_paise") or 0),
                made_at=datetime.fromisoformat(latest["at"]),
                note=latest.get("note", ""),
                broken_count=count_broken(reference, path))
    return None if p.due(now, grace) else p


def settle(reference: str, now: datetime, paid: bool,
           cfg: dict | None = None, path: Path | None = None) -> str | None:
    """Close out a promise whose date has passed. Kept, or broken.

    `paid` comes from the recovery ledger, never from the calendar: a promise
    is kept when the money arrives, and a date passing proves nothing either
    way.
    """
    grace = int(((cfg or {}).get("promise_to_pay") or {}).get("grace_days", 1))
    latest: dict | None = None
    for e in _for(reference, path):
        latest = e
    if not latest or latest["action"] != MADE:
        return None
    p = Promise(reference=reference,
                pay_by=datetime.fromisoformat(latest["pay_by"]),
                amount_paise=int(latest.get("amount_paise") or 0),
                made_at=datetime.fromisoformat(latest["at"]))
    if not p.due(now, grace):
        return None
    action = KEPT if paid else BROKEN
    _append(PromiseEvent(at=now, reference=reference, action=action,
                         pay_by=latest["pay_by"],
                         amount_paise=p.amount_paise,
                         note="payment arrived" if paid
                              else "the promised date passed unpaid"), path)
    return action


def cancel(reference: str, now: datetime, note: str, recorded_by: str,
           path: Path | None = None) -> None:
    if not note or not recorded_by:
        raise PromiseRefused("cancelling a promise requires a note and a name")
    _append(PromiseEvent(at=now, reference=reference, action=CANCELLED,
                         note=note, recorded_by=recorded_by), path)


def holds(reference: str, now: datetime, cfg: dict | None = None,
          path: Path | None = None) -> tuple[bool, str]:
    """Is the ladder paused for this case, and why?

    The single question `campaign.advance` needs to ask.
    """
    p = active(reference, now, cfg, path)
    if p is None:
        return False, ""
    return True, (f"they promised to pay by {p.pay_by:%d %b}"
                  + (f" ({p.note})" if p.note else ""))


def summary(now: datetime, cfg: dict | None = None,
            path: Path | None = None) -> dict:
    rows = read(path)
    refs = {r["reference"] for r in rows}
    live = [p for p in (active(r, now, cfg, path) for r in refs) if p]
    return {
        "active": len(live),
        "amount_paise": sum(p.amount_paise for p in live),
        "kept": sum(1 for r in rows if r["action"] == KEPT),
        "broken": sum(1 for r in rows if r["action"] == BROKEN),
        "soonest": min((p.pay_by.isoformat() for p in live), default=None),
    }
