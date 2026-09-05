"""Cached JSONL reads.

Every ledger predicate in this system -- has this succeeded, is it stopped, how
many attempts, when was the last contact -- is answered by reading and parsing
the entire file. That is fine at ten calls and quadratic at ten thousand: the
14-day projection runs 337 passes over 34 items, and each of those asks the
ledger several questions, so a 1,700-line file gets parsed some nineteen million
lines' worth. The console build stopped completing.

The fix is a cache keyed on the file's identity AND its state: path, mtime and
size together. An append changes both mtime and size, so the cache invalidates
exactly when the file changes and never serves a stale answer to a process that
has just written. That property is what makes this safe to put underneath an
append-only ledger -- the alternative, a time-based TTL, would occasionally
return a ledger missing the row we wrote a millisecond ago, which is precisely
the class of bug this project has spent its time removing.

Cached lists are returned by reference for speed, so callers must treat them as
read-only. Every caller here does; none mutates a ledger row.
"""
from __future__ import annotations

import json
from pathlib import Path

_CACHE: dict[Path, tuple[int, int, list[dict]]] = {}
_MAX = 32


def read_jsonl(path: Path) -> list[dict]:
    """Parse a JSONL file, reusing the last parse when the file has not moved."""
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        _CACHE.pop(p, None)
        return []

    key = (st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(p)
    if hit is not None and (hit[0], hit[1]) == key:
        return hit[2]

    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if len(_CACHE) >= _MAX:
        _CACHE.clear()          # tiny and rarely hit; simpler than an LRU
    _CACHE[p] = (st.st_mtime_ns, st.st_size, rows)
    return rows


_INDEX: dict[tuple, dict[str, list[dict]]] = {}


def index_by(path: Path, field: str) -> dict[str, list[dict]]:
    """Group a JSONL file by one field, cached like `read_jsonl`.

    The caching above removes the repeated PARSE; this removes the repeated
    SCAN. `events_for(reference)` filters the whole ledger linearly, and it is
    called several times per sequence per pass -- so a 14-day projection over
    34 items performs tens of millions of row visits looking for a handful.
    Grouping once per file version turns every one of those into a dict lookup.

    Returned lists are shared, so callers must treat them as read-only.
    """
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        return {}
    key = (p, st.st_mtime_ns, st.st_size, field)
    hit = _INDEX.get(key)
    if hit is not None:
        return hit
    out: dict[str, list[dict]] = {}
    for row in read_jsonl(p):
        out.setdefault(row.get(field), []).append(row)
    if len(_INDEX) >= _MAX:
        _INDEX.clear()
    _INDEX[key] = out
    return out


def invalidate(path: Path | None = None) -> None:
    """Drop cached parses. Only needed if a file is written behind our back."""
    if path is None:
        _CACHE.clear()
        _INDEX.clear()
    else:
        _CACHE.pop(Path(path), None)
        for k in [k for k in _INDEX if k[0] == Path(path)]:
            _INDEX.pop(k, None)
