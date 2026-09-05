"""Seed a payments book so the degradation detector has something to look at.

    python -m scripts.seed_traffic                 # show what it would write
    python -m scripts.seed_traffic --execute
    python -m scripts.seed_traffic --execute --healthy   # no incident at all

WHY. The degradation loop is not broken and never was -- it is starved. The
binomial CUSUM needs roughly thirty attempts in one segment before a rate shift
is distinguishable from noise, and the live Razorpay test account has six
payments in total, its busiest cell holding one. There is no payments-creation
API on this key to fix that with (`POST /payments/create/upi` returns 404, S2S
is not enabled), and a merchant's checkout volume is not something a sandbox
can be asked for.

So the book is ours, exactly like the subscription book. `src/ingest/
traffic_source.py` prefers the live account and falls back to this file, so the
day there is real volume the loop reads it instead with no code change.

THE INCIDENT IS NOT LABELLED IN THE BOOK. Every row is an ordinary payment with
an ordinary Razorpay error signature. What marks the incident window is only
that there are more failures in one cell -- which is precisely what the
detector has to notice by itself. The ground truth is printed here, once, so a
reader can grade the answer; it is never written into the data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from src.sim.traffic import generate

BOOK = Path("data/live/traffic.jsonl")


def arg(name, default=None, cast=str):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return default


def main() -> None:
    pays, truth = generate(
        minutes=arg("--minutes", 240, int),
        per_minute=arg("--rate", 9.0, float),
        seed=arg("--seed", 7, int),
        incident="--healthy" not in sys.argv)

    failed = sum(1 for p in pays if p.failed)
    print("SEED A LOCAL PAYMENTS BOOK")
    print("  The degradation detector needs ~30 attempts in one segment. The")
    print("  live account has 6 payments and no API to create more, so this")
    print("  book is ours -- and the live account is still read first.")
    print()
    print(f"  {len(pays)} payments over "
          f"{(pays[-1].created_at - pays[0].created_at).total_seconds()/3600:.1f}h, "
          f"{failed} failed ({failed/len(pays):.1%})")
    if truth:
        print(f"  INJECTED  {truth.describe()}")
        print("            printed here only; nothing in the file marks it")
    else:
        print("  no incident injected -- healthy traffic")
    print()

    if "--execute" not in sys.argv:
        print(f"  nothing written. Pass --execute to write {BOOK}.")
        return

    BOOK.parent.mkdir(parents=True, exist_ok=True)
    with BOOK.open("w", encoding="utf-8") as fh:
        for p in pays:
            fh.write(json.dumps({
                "payment_id": p.payment_id,
                "created_at": p.created_at.isoformat(),
                "amount_paise": p.amount_paise,
                "status": p.status, "method": p.method, "bank": p.bank,
                "wallet": p.wallet, "vpa": p.vpa,
                "error_code": p.error_code, "error_source": p.error_source,
                "error_step": p.error_step, "error_reason": p.error_reason,
                "order_id": p.order_id,
            }) + "\n")
    print(f"  wrote {BOOK}  ({len(pays)} rows)")
    print()
    print("  Next:  python -m scripts.ops degradation")


if __name__ == "__main__":
    main()
