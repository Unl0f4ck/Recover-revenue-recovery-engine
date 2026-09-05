"""The manual-review queue. Where escalation goes.

Both Etherlabs and Kill Bill treat manual review as a first-class destination:
Etherlabs carries a manual-review flag on every decision record, Kill Bill
exposes an endpoint for inspecting the payment method behind a failed payment
so a human can judge it.

Ours escalated to a `human_review` channel, stopped the sequence, and then
nothing. There was no queue to open, no way to act on an item, no ageing, and
the console showed a count with no list behind it. Escalation that goes nowhere
is a stopping rule wearing a handoff's clothes -- it looks like the system
handed the problem to a person, and in fact it dropped it.

A queue item is derived, not stored twice. The dunning ledger already records
every sequence that stopped at a terminal channel, so the OPEN queue is
computed from it; only the human's side -- claimed, resolved, with a note --
is recorded here. That keeps one source of truth for what the system did and
adds a second, separate one for what a person decided about it.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import dunning as D
from . import ledger as L
from .jsonl import read_jsonl

REVIEW = Path("data/live/review.jsonl")

CLAIMED = "claimed"
RESOLVED = "resolved"
REOPENED = "reopened"
DISMISSED = "dismissed"          # looked at, nothing to do


@dataclass(frozen=True)
class ReviewAction:
    at: datetime
    reference: str
    action: str
    who: str
    note: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ReviewItem:
    reference: str
    sequence_id: str
    at_risk_paise: int
    decline_class: str
    escalated_at: datetime
    stop_reason: str
    attempts_made: int
    customer_ref: str | None = None
    state: str = "open"              # open | claimed | resolved | dismissed
    claimed_by: str | None = None
    note: str = ""

    def age(self, now: datetime) -> timedelta:
        return now - self.escalated_at

    def ageing_band(self, now: datetime) -> str:
        """What an operator sorts by. Money matters, but a week-old escalation
        on a small amount is still a customer nobody answered."""
        d = self.age(now).days
        return "over a week" if d >= 7 else ("over a day" if d >= 1 else "today")


def read(path: Path | None = None) -> list[dict]:
    p = path or REVIEW
    return read_jsonl(p)


def _append(a: ReviewAction, path: Path | None = None) -> None:
    p = path or REVIEW
    p.parent.mkdir(parents=True, exist_ok=True)
    d = asdict(a)
    d["at"] = a.at.isoformat()
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(d) + "\n")


def claim(reference: str, who: str, at: datetime, note: str = "",
          path: Path | None = None) -> None:
    if not who:
        raise ValueError("claiming a review item requires a name")
    _append(ReviewAction(at, reference, CLAIMED, who, note), path)


def resolve(reference: str, who: str, at: datetime, note: str,
            path: Path | None = None) -> None:
    """Close an item. A note is required.

    An escalation resolved without a reason tells the next person nothing, and
    the whole point of routing this to a human was to capture the judgement
    the system could not make.
    """
    if not who or not note:
        raise ValueError("resolving a review item requires a name and a note")
    _append(ReviewAction(at, reference, RESOLVED, who, note), path)


def dismiss(reference: str, who: str, at: datetime, note: str,
            path: Path | None = None) -> None:
    if not who or not note:
        raise ValueError("dismissing a review item requires a name and a note")
    _append(ReviewAction(at, reference, DISMISSED, who, note), path)


def reopen(reference: str, who: str, at: datetime, note: str,
           path: Path | None = None) -> None:
    if not who or not note:
        raise ValueError("reopening a review item requires a name and a note")
    _append(ReviewAction(at, reference, REOPENED, who, note), path)


def _human_state(reference: str, path: Path | None = None) -> tuple[str, str | None, str]:
    """Latest human action on this item. Append-only, so last wins."""
    state, who, note = "open", None, ""
    for a in read(path):
        if a.get("reference") != reference:
            continue
        act = a.get("action")
        if act == CLAIMED:
            state, who = "claimed", a.get("who")
        elif act == RESOLVED:
            state, who, note = "resolved", a.get("who"), a.get("note", "")
        elif act == DISMISSED:
            state, who, note = "dismissed", a.get("who"), a.get("note", "")
        elif act == REOPENED:
            state, who, note = "open", None, a.get("note", "")
    return state, who, note


def queue(now: datetime, cfg: dict | None = None,
          ledger_path: Path | None = None, path: Path | None = None,
          include_closed: bool = False) -> list[ReviewItem]:
    """Everything a human is expected to look at.

    Derived from the dunning ledger rather than stored separately: the ledger
    already knows which sequences stopped at a terminal channel, and a second
    copy would be a second thing to keep in sync.
    """
    from .campaign import rebuild
    from .declines import load_declines
    cfg = cfg or load_declines()

    stopped: dict[str, dict] = {}
    for e in L.read(ledger_path):
        if e["event"] == L.SEQUENCE_STOPPED:
            stopped[e["reference"]] = e

    out: list[ReviewItem] = []
    for ref, e in stopped.items():
        reason = (e.get("extra") or {}).get("stop_reason", "")
        seq = rebuild(ref, cfg, ledger_path)
        if seq is None:
            continue
        n = D.current_attempt_no(ref, ledger_path)
        # A sequence that ended at a HUMAN_REVIEW rung is the queue. A write-off
        # is not: it ended because the ladder said stop, and nobody is waiting.
        idx = min(n, len(seq.steps) - 1)
        channel = seq.steps[idx].channel if seq.steps else ""
        if reason != "human_review_required" and (reason != D.STOP_TERMINAL_CHANNEL or channel != "human_review"):
            continue
        state, who, note = _human_state(ref, path)
        if state in ("resolved", "dismissed") and not include_closed:
            continue
        out.append(ReviewItem(
            reference=ref, sequence_id=seq.sequence_id,
            at_risk_paise=seq.at_risk_paise,
            decline_class=seq.classification.decline_class,
            escalated_at=datetime.fromisoformat(e["at"]),
            stop_reason=reason, attempts_made=n,
            customer_ref=seq.customer_ref,
            state=state, claimed_by=who, note=note))

    # Oldest first within value bands: a large item escalated an hour ago is
    # less urgent than a smaller one nobody has answered for a week.
    out.sort(key=lambda i: (-i.age(now).days, -i.at_risk_paise))
    return out


def summary(now: datetime, cfg: dict | None = None,
            ledger_path: Path | None = None,
            path: Path | None = None) -> dict:
    q = queue(now, cfg, ledger_path, path)
    bands: dict[str, int] = {}
    for i in q:
        b = i.ageing_band(now)
        bands[b] = bands.get(b, 0) + 1
    return {
        "open": sum(1 for i in q if i.state == "open"),
        "claimed": sum(1 for i in q if i.state == "claimed"),
        "at_risk_paise": sum(i.at_risk_paise for i in q),
        "oldest_days": max((i.age(now).days for i in q), default=0),
        "ageing": sorted(bands.items(), key=lambda kv: -kv[1]),
    }
