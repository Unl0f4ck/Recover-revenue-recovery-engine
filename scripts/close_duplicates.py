"""Close campaigns that were opened against invoice-backed orders.

    python -m scripts.close_duplicates            # show what would close
    python -m scripts.close_duplicates --execute

Every Razorpay invoice creates a backing order, and while the invoice is unpaid
that order is unpaid too. Until this was caught, the ingestion counted both:
the same money appeared once as an overdue receivable and once as an abandoned
checkout, inflating the book by the whole receivables balance -- Rs 2,96,050
across 16 orders.

The ingestion is fixed. This closes the campaigns opened before it was.

WHY A STOP EVENT AND NOT A DELETE. The ledger is append-only, and that is not a
technicality to route around when the wrong rows are ours. A campaign that was
opened DID happen; pretending otherwise would make the audit trail agree with
the current code rather than with history, which is the one thing an audit
trail must never do. So each duplicate is closed with a reason that says
exactly what happened, and both the opening and the correction stay visible.
"""
from __future__ import annotations

import sys
from datetime import datetime

from src.execution.razorpay import _call, load_env
from src.recovery import dunning as D
from src.recovery import ledger as L
from src.recovery.campaign import IST, rebuild
from src.recovery.declines import load_declines

STOP_REASON = "superseded_by_invoice"


def main() -> None:
    execute = "--execute" in sys.argv
    env = load_env()
    cfg = load_declines()
    now = datetime.now(IST)

    invoices = _call("GET", "/invoices?count=100", None, env).get("items", [])
    backed = {i["order_id"]: i for i in invoices if i.get("order_id")}

    open_refs = L.open_sequences()
    dupes = [r for r in open_refs if r in backed]

    print("CLOSE DUPLICATE CAMPAIGNS")
    print(f"  mode        {'EXECUTE' if execute else 'dry run'}")
    print(f"  open now    {len(open_refs)} campaigns")
    print(f"  duplicates  {len(dupes)} opened against an invoice-backed order")
    print()

    total = 0
    for ref in sorted(dupes):
        seq = rebuild(ref, cfg)
        if seq is None:
            continue
        inv = backed[ref]
        total += seq.at_risk_paise
        print(f"  {ref:<26} Rs {seq.at_risk_paise/100:>10,.0f}  "
              f"-> {inv['id']} ({inv.get('status')})")
        if execute:
            D.stop(seq, STOP_REASON, now,
                   f"the same money is tracked as invoice {inv['id']}; this "
                   f"order is only the invoice's backing object",
                   path=None)

    print()
    print(f"  Rs {total/100:,.0f} was being counted twice")
    if execute:
        print(f"  closed {len(dupes)} campaign(s). The opening events remain in "
              f"the ledger --\n  a campaign that was opened did happen, and an "
              f"audit trail that agrees with\n  the current code rather than "
              f"with history is worth nothing.")
    else:
        print("  dry run -- pass --execute to close them.")


if __name__ == "__main__":
    main()
