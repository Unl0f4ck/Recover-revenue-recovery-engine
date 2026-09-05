"""Seed a subscription book this project holds itself.

    python -m scripts.seed_subscriptions                # show what it would write
    python -m scripts.seed_subscriptions --execute
    python -m scripts.seed_subscriptions --execute --n 60 --seed 3

WHY NOT RAZORPAY. Subscriptions is not enabled on this account --
`/subscriptions` and `/plans` return 401 on a key that reads `/payments` and
`/customers` fine -- and it cannot be enabled from here. The receivables stream
got real Razorpay invoices because the Invoices API was available;
`seed_receivables` created them and they are genuinely on the account. This is
the same idea against an API we cannot reach, so the book lives locally and is
served by `src/gateways/local.py`.

WHAT IS REAL AND WHAT IS NOT. The subscriptions are ours. The schedule, the
classification, the guards, the ledger and every stopping rule are the
production code, unchanged. No artefact here is ever `REAL` mode -- the local
gateway stamps `LOCAL` on everything it mints, so nothing in this book can be
confused with the Rs 28,433 that actually moved through Razorpay.

THE OUTCOME IS WRITTEN DOWN, NOT ROLLED. Every row carries `funds_return_at`:
the moment the money becomes available. A silent re-present captures if and
only if it happens at or after that moment, so the file fully determines what
the ladder will recover and a reader can predict every outcome before running
anything. That is not a simplification of the real dynamic -- it IS the real
dynamic. A silent retry persuades nobody; it is simply there when the balance
returns, which is the entire reason subscription dunning outperforms checkout
dunning.

The dates come from the decline class, and the classes come from
`config/declines.yaml`:

    SOFT_FUNDS        the customer is paid on a cycle. Days, not hours.
    SOFT_TECHNICAL    a rail was down. Hours.
    AUTH              needs the customer back at a checkout; no silent rung
                      ever fixes it, so no funds date at all.
    HARD_INSTRUMENT   the card is dead. Never returns, whatever the ladder does.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from src.recovery.declines import load_declines

IST = timezone(timedelta(hours=5, minutes=30))
BOOK = Path("data/live/acme_subs.subscriptions.jsonl")

# A subscription book's decline mix, not a checkout book's: expired cards and
# insufficient funds dominate, interactive auth barely appears because a
# recurring charge skips it, and there is no checkout to abandon. Shared with
# the recovery-curve analysis rather than re-chosen here.
from scripts.recovery_curve import BOOK_B

# When the money comes back, per class. The single most consequential table in
# this file, and the reason the ladder's timing matters at all.
#
# SOFT_FUNDS is measured in DAYS because salaries arrive on a cycle -- which is
# exactly why the mandate ladder waits 4h, 24h, 72h and then a week rather than
# retrying every few minutes. HARD_INSTRUMENT and AUTH return None: no silent
# retry recovers them at any cadence, and letting them convert would raise the
# recovery rate above the ceiling docs/RECOVERY_RATE.md argues for.
FUNDS_RETURN_HOURS = {
    "SOFT_FUNDS":      (18, 240),      # payday, somewhere in the next 10 days
    "SOFT_TECHNICAL":  (1, 14),        # the rail comes back
    "VPA":             (24, 200),      # they fix the handle, or they do not
    "AUTH":            None,           # needs the customer, not a retry
    "HARD_INSTRUMENT": None,           # the card is dead
    "CUSTOMER_ABORTED": None,
}

# Not every case whose class could recover actually does. The per-class ceiling
# from the analysis decides which rows get a date at all -- without it every
# SOFT_FUNDS subscription would eventually pay and the book would recover ~100%.
from scripts.recovery_curve import RECOVERABLE

PLANS = [("plan_basic", 49900), ("plan_pro", 129900), ("plan_team", 349900),
         ("plan_scale", 899900)]
FIRST = ["rajat", "priya", "aditya", "meera", "farhan", "kavya", "ishan",
         "ananya", "vikram", "sneha", "arjun", "divya", "rohit", "nisha"]
LAST = ["sharma", "iyer", "khan", "reddy", "patel", "bose", "nair", "gupta",
        "menon", "shah", "verma", "das", "rao", "joshi"]


def arg(name, default=None, cast=str):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return default


def build(n: int, seed: int, now: datetime, cfg: dict) -> list[dict]:
    rng = np.random.default_rng(seed)
    reasons = {k: list(v.get("reasons") or [])
               for k, v in cfg["classes"].items()}
    classes = [c for c in BOOK_B if BOOK_B[c] > 0]
    w = np.array([BOOK_B[c] for c in classes], dtype=float)
    w = w / w.sum()

    out = []
    for i in range(1, n + 1):
        cls = classes[int(rng.choice(len(classes), p=w))]
        pool = reasons.get(cls) or ["payment_failed"]
        plan, amount = PLANS[int(rng.integers(len(PLANS)))]

        # The charge failed some hours ago; the book is discovered now.
        failed_at = now - timedelta(hours=float(rng.uniform(1, 96)))

        window = FUNDS_RETURN_HOURS.get(cls)
        recoverable = bool(rng.random() < RECOVERABLE.get(cls, 0.5))
        funds_at = None
        if window and recoverable:
            lo, hi = window
            funds_at = (failed_at
                        + timedelta(hours=float(rng.uniform(lo, hi))))

        # A dead instrument is a mandate that has stopped working. Recorded
        # separately from the funds date because it is a different fact: one is
        # "no money yet", the other is "no way to take it".
        mandate_live = cls != "HARD_INSTRUMENT"

        name = (f"{FIRST[int(rng.integers(len(FIRST)))]}."
                f"{LAST[int(rng.integers(len(LAST)))]}{i}")
        out.append({
            "id": f"sub_local_{i:04d}",
            "plan_id": plan,
            "amount_paise": amount,
            "status": "halted" if rng.random() < 0.45 else "pending",
            "decline_class": cls,
            "error_reason": pool[int(rng.integers(len(pool)))],
            "customer_ref": f"{name}@sim.invalid",
            "failed_at": failed_at.isoformat(),
            "paid_count": int(rng.integers(1, 26)),
            "remaining_count": int(rng.integers(1, 19)),
            "mandate_live": mandate_live,
            # THE WHOLE OUTCOME OF THIS ROW, in one field. Null means no silent
            # retry will ever capture it.
            "funds_return_at": funds_at.isoformat() if funds_at else None,
        })
    return out


def main() -> None:
    cfg = load_declines()
    n = arg("--n", 45, int)
    seed = arg("--seed", 20260902, int)
    now = datetime.now(IST)
    rows = build(n, seed, now, cfg)

    total = sum(r["amount_paise"] for r in rows)
    recoverable = [r for r in rows if r["funds_return_at"]]
    print("SEED A LOCAL SUBSCRIPTION BOOK")
    print(f"  Razorpay Subscriptions is not enabled on this account, so this")
    print(f"  book is ours and is served by the `local` gateway. Nothing here")
    print(f"  is REAL money and every artefact is stamped LOCAL.")
    print()
    print(f"  {len(rows)} subscriptions, Rs {total/100:,.0f} per cycle")
    print(f"  {len(recoverable)} carry a funds-return date; "
          f"{len(rows)-len(recoverable)} never recover at any cadence")
    print()
    by: dict[str, int] = {}
    for r in rows:
        by[r["decline_class"]] = by.get(r["decline_class"], 0) + 1
    for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
        live = sum(1 for r in rows
                   if r["decline_class"] == k and r["funds_return_at"])
        print(f"    {k:<18}{v:>4}   {live} will return, {v-live} will not")
    print()
    for r in rows[:5]:
        when = (r["funds_return_at"] or "")[:16].replace("T", " ")
        print(f"    {r['id']}  Rs {r['amount_paise']/100:>8,.0f}  "
              f"{r['decline_class']:<17}"
              + (f"funds back {when}" if when else "never returns"))
    if len(rows) > 5:
        print(f"    ... {len(rows)-5} more")
    print()

    if "--execute" not in sys.argv:
        print(f"  nothing written. Pass --execute to write {BOOK}.")
        return

    BOOK.parent.mkdir(parents=True, exist_ok=True)
    with BOOK.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"  wrote {BOOK}  ({len(rows)} rows)")
    print()
    print("  Next:  python -m scripts.run_dunning --merchant acme_subs")


if __name__ == "__main__":
    main()
