"""Cached YAML config reads.

Every config loader in this system -- `load_declines`, `load_workflows`,
`load_policy` -- read the file and re-parsed it on every call. That is
invisible at a handful of calls and dominant at thousands: `may_open` consults
the per-workflow exposure floor for every item on every pass, and `floor_for`
calls `load_workflows()` each time. A 240-case batch played forward over three
weeks spends most of its wall clock in the YAML parser, re-reading four
kilobytes of unchanged configuration a few thousand times.

WHY NOT `lru_cache`. Because it would break the kill switch. `policy.yaml`
carries `bounds.global_kill_switch`, and the whole point of that control is
that an operator can set it while a run is in flight and have the next pass
stop. A permanent memo would pin the value read at import time and the switch
would silently stop working -- which is exactly the failure this project has
already had once, when the kill switch was plumbed as a parameter that nothing
ever read.

So the cache key is the file's identity AND its state: path, mtime and size
together, the same scheme `src/recovery/jsonl.py` uses underneath the ledger.
Editing a config changes both mtime and size, the entry invalidates, and the
next read sees the new value. Nothing observable changes except the speed.

Parsed documents are returned BY REFERENCE, so a caller that mutates one
mutates every later reader's copy. Every loader here is read-only, and treating
config as immutable is the right discipline anyway -- but it is the reason this
returns the same object rather than a copy, and it is worth knowing.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_CACHE: dict[Path, tuple[tuple[int, int], dict]] = {}
_MAX = 32


def load_yaml(path: Path) -> dict:
    """Parse a YAML file, reusing the last parse while the file is unchanged."""
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        _CACHE.pop(p, None)
        raise

    key = (st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(p)
    if hit is not None and hit[0] == key:
        return hit[1]

    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    if len(_CACHE) >= _MAX:
        _CACHE.clear()          # tiny and rarely hit; simpler than an LRU
    _CACHE[p] = (key, doc)
    return doc


def invalidate(path: Path | None = None) -> None:
    """Drop cached parses. Only needed if a file is written behind our back."""
    if path is None:
        _CACHE.clear()
    else:
        _CACHE.pop(Path(path), None)
