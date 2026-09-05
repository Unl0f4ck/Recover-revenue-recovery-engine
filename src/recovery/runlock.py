"""One writer at a time.

Both PostgreSQL-backed systems in docs/PRIOR_ART.md solve this with
`FOR UPDATE SKIP LOCKED` retry leases -- recoup and Etherlabs independently.
The property they are buying is that two workers cannot pick up the same
recovery attempt, because if they do, the customer gets two messages and
possibly two charges.

We store state in a JSONL file, so there is no row to lock. But the exposure is
identical and arguably worse: `run_dunning --execute` twice at once, or a cron
firing while a human runs it by hand, and both processes read the same ledger,
both see the same step due, both write ahead, and both create a link. The
idempotency key protects the PROVIDER from double-charging; nothing protected
the customer from two messages, and nothing protected the ledger from
interleaved writes.

This is the file-based equivalent: an exclusive lock held for the length of a
pass. It is deliberately unforgiving -- a second runner does not queue, it
refuses and says who holds the lock. A recovery pass that waits its turn and
then fires a message the first pass already sent is the bug, not the fix.

Stale locks are reclaimed by age rather than by checking whether the pid is
alive: a pid check is wrong across containers, and a lock older than any
plausible pass is either a crash or a hang, both of which want the same answer.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCK = Path("data/live/dunning.lock")
STALE_AFTER = timedelta(minutes=30)


class LockHeld(RuntimeError):
    """Another pass is running. Deliberately not retried."""


def _read(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return None


@contextmanager
def exclusive(path: Path | None = None, now: datetime | None = None,
              label: str = ""):
    """Hold the run lock, or raise LockHeld.

    `O_CREAT | O_EXCL` is the whole mechanism: on every platform we care about
    it either creates the file or fails, atomically, with no window between the
    check and the write. A `Path.exists()` test followed by a write has exactly
    that window, and it is the window two crons collide in.
    """
    p = path or LOCK
    p.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)

    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        held = _read(p) or {}
        at = held.get("at")
        age = None
        if at:
            try:
                age = now - datetime.fromisoformat(at)
            except ValueError:
                age = None
        if age is not None and age > STALE_AFTER:
            # Older than any pass could legitimately take: a crash or a hang.
            # Both want the lock released, and neither is helped by waiting.
            p.unlink(missing_ok=True)
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        else:
            raise LockHeld(
                f"another recovery pass holds the lock "
                f"(pid {held.get('pid', '?')}, started {at or 'unknown'}"
                + (f", {held['label']}" if held.get("label") else "")
                + f"). Wait for it, or delete {p} if you are certain it died.")

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "at": now.isoformat(),
                       "label": label}, fh)
        yield p
    finally:
        p.unlink(missing_ok=True)


def is_held(path: Path | None = None) -> bool:
    return (path or LOCK).exists()
