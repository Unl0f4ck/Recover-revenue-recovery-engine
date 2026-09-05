"""Append-only dunning ledger, and the idempotency keys that make retries safe.

Two invariants live here, both taken from prior art, both regression-tested:

  ONE SUCCESS PER REFERENCE.  recoup enforces this with a partial unique index
  so that double-charging is unrepresentable at the schema level. We have a
  JSONL file rather than PostgreSQL, so the equivalent is `has_succeeded()`,
  consulted before every attempt is authorised. It is the same idea one layer
  up: the question "has this already been paid" is asked from durable storage,
  never from in-process state, because a recovery link outlives the process
  that created it.

  THE KEY SURVIVES THE FAILURE.  This is medusajs/medusa#16292. Medusa forwards
  a Capture row's ID to the provider as the idempotency key, then DELETES that
  row when the provider call fails. The retry creates a new row, mints a new
  ID, and sends a key the provider has never seen -- so the provider cannot
  deduplicate, and the customer is charged twice. Idempotency discarded on
  error is idempotency for the happy path only, which is the one path that
  never needed it.

  The fix here is to make the key DERIVED rather than stored: it is a pure
  function of (reference, attempt_no, policy_version), so it cannot be lost by
  any failure, crash or restart -- recomputing it after a total process loss
  yields the identical string. The write-ahead record below is then belt to
  that braces: it makes the intent auditable, but correctness does not depend
  on it having been written.

  The corollary matters as much as the rule: a TRANSPORT failure must not
  advance `attempt_no`. Advancing the counter is exactly what mints a new key.
  Only a *decided* outcome from the gateway -- a real decline -- advances the
  sequence. See `dunning.record_transport_failure`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .jsonl import index_by, read_jsonl

LEDGER = Path("data/live/dunning_ledger.jsonl")

# Event vocabulary. Append-only: an event is never rewritten, and a correction
# is itself an event.
SEQUENCE_OPENED = "sequence_opened"
ATTEMPT_SCHEDULED = "attempt_scheduled"
ATTEMPT_INFLIGHT = "attempt_inflight"        # written-ahead, BEFORE the call
ATTEMPT_SUCCEEDED = "attempt_succeeded"
ATTEMPT_FAILED = "attempt_failed"            # gateway decided: a real decline
ATTEMPT_DELIVERED = "attempt_delivered"      # contact made; recovery not yet known
ATTEMPT_AMBIGUOUS = "attempt_ambiguous"      # transport died; outcome unknown
ATTEMPT_UNEXECUTABLE = "attempt_unexecutable"
RECONCILED = "reconciled"
CONTACT_DEFERRED = "contact_deferred"
SEQUENCE_STOPPED = "sequence_stopped"


def idempotency_key(reference: str, attempt_no: int, policy_version: str) -> str:
    """Deterministic, and that is the whole point.

    Derived from durable facts only, so it is identical on every recomputation
    for the same attempt and different for the next one. Nothing has to remember
    it; nothing can lose it.

    `policy_version` participates so that changing the schedule cannot silently
    collide a new attempt with an old one's key on the provider side.
    """
    raw = f"{policy_version}|{reference}|{attempt_no}"
    return "rcv_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class LedgerEvent:
    at: datetime
    event: str
    reference: str              # the at-risk object: order_id / payment_id
    sequence_id: str
    attempt_no: int
    policy_version: str
    decline_class: str | None = None
    channel: str | None = None
    rail: str | None = None
    idempotency_key: str | None = None
    amount_paise: int = 0
    execution_mode: str = "SIMULATED"
    execution_reference: str | None = None
    execution_url: str | None = None
    detail: str = ""
    extra: dict = field(default_factory=dict)


def sequence_id(reference: str, policy_version: str) -> str:
    return "seq_" + hashlib.sha256(
        f"{policy_version}|{reference}".encode()).hexdigest()[:20]


def to_json(e: LedgerEvent) -> dict:
    d = asdict(e)
    d["at"] = e.at.isoformat()
    return d


def append(event: LedgerEvent, path: Path | None = None) -> None:
    """The only write operation this module offers.

    There is no update and no delete, by design -- an immutable ledger is what
    makes the audit trail worth anything. A wrong record is corrected by
    appending the correction, so the mistake stays visible.
    """
    p = path or LEDGER
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(to_json(event)) + "\n")


def read(path: Path | None = None) -> list[dict]:
    p = path or LEDGER
    return read_jsonl(p)


def events_for(reference: str, path: Path | None = None) -> list[dict]:
    """Every event for one reference. Indexed, not scanned.

    This is the hottest query in the system -- `has_succeeded`, `is_stopped`,
    `attempts_made`, `contacts_made` and `last_attempt_at` all go through it,
    several times per sequence per pass.
    """
    return index_by(path or LEDGER, "reference").get(reference, [])


def has_succeeded(reference: str, path: Path | None = None) -> bool:
    """At most one success per reference. Asked of the ledger, not of memory."""
    return any(e["event"] == ATTEMPT_SUCCEEDED for e in events_for(reference, path))


def is_stopped(reference: str, path: Path | None = None) -> bool:
    return any(e["event"] == SEQUENCE_STOPPED for e in events_for(reference, path))


def attempts_made(reference: str, path: Path | None = None) -> int:
    """How many attempts have reached a DECIDED outcome.

    DELIVERED counts. A recovery link that was successfully created is an
    attempt that happened -- the customer has it -- even though whether they pay
    is unknown for days. Not counting it would re-create the link on every pass.

    UNEXECUTABLE counts, and this was a bug for a while. A silent retry needs a
    saved token or e-mandate; without one it can never run on this account. If
    such a step does not advance the ladder, the sequence retries it on every
    pass forever and NEVER REACHES the contacting rungs below it -- a silent
    deadlock that only shows up on the `fast` and `slow` schedules, which are
    the two that actually matter for failed payments. The step is skipped, and
    skipping is a form of progress. It is still not a customer contact, so it
    never counts toward the compliance ceiling, and it never counts as a
    recovery attempt in the funnel.

    AMBIGUOUS does not count. An attempt whose result we never learned has not
    been made, and counting it would both burn a slot the customer never saw
    and -- worse -- advance the attempt number, minting a new idempotency key
    for what is still the same attempt (#16292). That is the one case where
    standing still is correct.
    """
    # DISTINCT attempt numbers, not decided events. One attempt can produce
    # several decided events -- a link is DELIVERED, and days later the same
    # attempt is reconciled to SUCCEEDED. Counting events rather than attempts
    # made a recovered campaign read "attempt 3 of 4" after a single contact,
    # and marked a rung of the ladder complete that had never run.
    return len({e["attempt_no"] for e in events_for(reference, path)
                if e["event"] in (ATTEMPT_SUCCEEDED, ATTEMPT_FAILED,
                                  ATTEMPT_DELIVERED, ATTEMPT_UNEXECUTABLE)})


def contacts_made(reference: str, contacting: list[str],
                  path: Path | None = None) -> int:
    """Customer contacts on this reference, for the compliance ceiling.

    Counts write-ahead INFLIGHT records rather than successes: a message we
    attempted to send is a message the customer may have received, and the
    compliance question is how often we reached at them, not how often it worked.
    """
    return sum(1 for e in events_for(reference, path)
               if e["event"] == ATTEMPT_INFLIGHT and e.get("channel") in contacting)


def recovered_paise(path: Path | None = None) -> int:
    return sum(int(e.get("amount_paise", 0)) for e in read(path)
               if e["event"] == ATTEMPT_SUCCEEDED)


def open_sequences(path: Path | None = None) -> set[str]:
    """References with a sequence opened and not yet stopped."""
    opened, stopped = set(), set()
    for e in read(path):
        if e["event"] == SEQUENCE_OPENED:
            opened.add(e["reference"])
        elif e["event"] == SEQUENCE_STOPPED:
            stopped.add(e["reference"])
    return opened - stopped
