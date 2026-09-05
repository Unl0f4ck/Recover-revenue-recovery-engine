"""Payments for the degradation detector: the live account, or our own book.

THE LIVE ACCOUNT IS ALWAYS TRIED FIRST and always will be. It is read, counted,
and used whenever it can actually support a decision -- the fallback triggers
on volume, not on preference, so the day this merchant has real traffic the
loop reads it and the local book stops being consulted with no code change.

WHY A FALLBACK AT ALL. A detector with no data and a detector that is broken
look identical from outside: both report nothing. That ambiguity is what made
this loop read as a defect for a week, when the truth was six payments on a
sandbox. Reporting WHICH source answered, every time, is what separates the two.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .razorpay_source import RealPayment, fetch_payments

BOOK = Path("data/live/traffic.jsonl")


@dataclass(frozen=True)
class Traffic:
    payments: list[RealPayment]
    source: str                 # "live" | "local"
    live_count: int
    note: str = ""


def read_local(path: Path | None = None) -> list[RealPayment]:
    p = Path(path or BOOK)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out.append(RealPayment(
            payment_id=r["payment_id"],
            created_at=datetime.fromisoformat(r["created_at"]),
            amount_paise=int(r["amount_paise"]), status=r["status"],
            method=r.get("method"), bank=r.get("bank"),
            wallet=r.get("wallet"), vpa=r.get("vpa"),
            error_code=r.get("error_code"), error_source=r.get("error_source"),
            error_step=r.get("error_step"), error_reason=r.get("error_reason"),
            order_id=r.get("order_id")))
    return out


def collect(limit: int = 500, min_volume: int = 30,
            env: dict | None = None, allow_local: bool = True) -> Traffic:
    """Live traffic if it can support a decision, otherwise the local book."""
    try:
        live = fetch_payments(limit, env)
    except Exception:                                    # noqa: BLE001
        live = []

    by_cell: dict[tuple, int] = {}
    for p in live:
        k = (p.issuer, p.method)
        by_cell[k] = by_cell.get(k, 0) + 1
    biggest = max(by_cell.values(), default=0)

    if biggest >= min_volume or not allow_local:
        return Traffic(live, "live", len(live),
                       f"live account, busiest cell {biggest}")
    local = read_local()
    if not local:
        return Traffic(live, "live", len(live),
                       f"live account, busiest cell {biggest} -- too thin to "
                       f"decide, and no local book to fall back to")
    return Traffic(local, "local", len(live),
                   f"live account has {len(live)} payments, busiest cell "
                   f"{biggest} of the {min_volume} needed; read the local "
                   f"book instead")
