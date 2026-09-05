"""Run the recovery agent over the live account and report measured money.

This is the deliverable the track brief asks for: money recovered across a
batch, with compliant escalation, stopping rules and an audit trail.

    python -m scripts.run_recovery_batch                 # dry run, decisions only
    python -m scripts.run_recovery_batch --execute       # create real recovery links
    python -m scripts.run_recovery_batch --measure       # read outcomes back
    python -m scripts.run_recovery_batch --execute --max 6

`--as-of N` evaluates the batch as though N minutes have passed. The
abandonment grace period is 30 minutes, so a batch seeded moments ago has
nothing eligible yet. Nothing about the data is altered -- the orders are real
and genuinely unpaid; only the clock the eligibility rule is compared against
moves. Every report states the offset it used.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from src.recovery.runner import (IST, measure, run_batch, to_json, write_audit)
from src.ingest.razorpay_source import account_summary

OUT = Path("data/live")


def rs(paise) -> str:
    return f"Rs {paise/100:,.0f}"


def main() -> None:
    execute = "--execute" in sys.argv
    do_measure = "--measure" in sys.argv
    as_of = 0
    limit = None
    for i, a in enumerate(sys.argv):
        if a == "--as-of" and i + 1 < len(sys.argv):
            as_of = int(sys.argv[i + 1])
        if a == "--max" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    now = datetime.now(IST) + timedelta(minutes=as_of)

    print("RECOVERY BATCH  --  live Razorpay test-mode account")
    print(f"  mode        {'EXECUTE (real links)' if execute else 'dry run (no writes)'}")
    if as_of:
        print(f"  evaluated   as of now + {as_of} min "
              f"(abandonment grace period is 30 min)")
    acct = account_summary()
    print(f"  account     {acct['payments']} payments "
          f"({acct['failed']} failed), {acct['orders']} orders, "
          f"{acct['payment_links']} links")
    print()

    res = run_batch(dry_run=not execute, limit=limit, now=now)
    if do_measure:
        res = measure(res)

    print(f"REVENUE AT RISK   {rs(res.at_risk_total_paise)} across "
          f"{len(res.attempts)} items")
    for k, v in res.stream_totals.items():
        print(f"  {k:<20} n={v['n']:<4} {rs(v['at_risk_paise'])}")
    print()

    acted = [a for a in res.attempts if a.authorized]
    held = [a for a in res.attempts if not a.authorized]
    print(f"DECISIONS         {len(acted)} acted on, {len(held)} held back")
    print(f"  at risk acted   {rs(sum(a.at_risk_paise for a in acted))}")
    print(f"  at risk held    {rs(sum(a.at_risk_paise for a in held))}")
    print()

    if acted:
        print("ACTED")
        for a in acted[:12]:
            print(f"  {a.reference:<26} {a.action_executed:<22} "
                  f"{rs(a.at_risk_paise):>12}  [{a.execution_mode}]"
                  + (f"  {a.execution_url}" if a.execution_url else ""))
        if len(acted) > 12:
            print(f"  ... {len(acted)-12} more")
        print()

    reasons: dict[str, int] = {}
    for a in held:
        for r in (a.gate_reasons + a.bounds_fired) or ["no reason recorded"]:
            key = r.split(" Rs ")[0].split(" ")[0:3]
            reasons[" ".join(key)] = reasons.get(" ".join(key), 0) + 1
    if reasons:
        print("WHY THE REST WERE HELD BACK  (stopping rules)")
        for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {c:>4}  {r}")
        print()

    if do_measure:
        rec = [a for a in res.attempts if a.outcome == "recovered"]
        print("MEASURED OUTCOME  (read back from the Razorpay API)")
        print(f"  recovered       {rs(res.recovered_total_paise)} "
              f"from {len(rec)} of {len(acted)} attempts")
        print(f"  recovery rate   {res.recovery_rate:.1%} of all revenue at risk")
        if not rec:
            print("  nothing paid yet -- recovery links are live and awaiting payment")
        print()

    OUT.mkdir(parents=True, exist_ok=True)
    write_audit(OUT / "recovery_audit.jsonl", res)
    (OUT / "recovery_batch.json").write_text(
        json.dumps(to_json(res), indent=2), encoding="utf-8")
    print(f"audit trail   {OUT/'recovery_audit.jsonl'}  ({len(res.attempts)} records)")
    print(f"batch result  {OUT/'recovery_batch.json'}")


if __name__ == "__main__":
    main()
