"""Seed real invoices on the test account so the receivables stream has a book.

    python -m scripts.seed_receivables            # show what would be created
    python -m scripts.seed_receivables --execute

These are REAL Razorpay invoices, issued to the test account, with real short
URLs a person can open and pay. Nothing about them is faked except that we
created them rather than a merchant.

WHY THE ISSUE DATE IS BACKDATED. A receivable is overdue when the issue date
plus the agreed payment terms has passed. Razorpay accepts a past `date` on an
invoice -- which is what a real merchant does when they import a back book --
so an invoice dated three weeks ago on 14-day terms is *genuinely* overdue by
the same arithmetic the engine uses in production. Nothing about the clock is
faked; only the paperwork is older than the account.

`expire_by` cannot be backdated (Razorpay requires a future value), which is
exactly why the engine derives the due date from issue + terms instead of
reading `expire_by`. That field is a technical expiry for the link, not a
commercial one for the debt.
"""
from __future__ import annotations

import sys
import time

from src.execution.razorpay import RECOVERY_TAG, _call, load_env
from src.recovery.declines import load_declines

DAY = 86400

# A plausible small-business receivables book: a few big and slow, several
# small and recent, one only just past its terms.
BOOK = [
    # (name, email, contact, paise, days since issue, description)
    # Amounts are realistic for a small-business receivables book. Two are
    # deliberately below the exposure floor: a book where everything clears the
    # floor never demonstrates the floor, and the floor is the control that
    # stops the engine spending a contact it cannot earn back.
    ("Meera Iyer",     "meera@example.com",  "+919812345671", 92_50_000, 34, "Consulting retainer, August"),
    ("Rajat Khanna",   "rajat@example.com",  "+919812345672", 48_00_000, 27, "Design sprint, phase two"),
    ("Sunita Rao",     "sunita@example.com", "+919812345673", 31_75_000, 24, "Annual licence renewal"),
    ("Vikram Desai",   "vikram@example.com", "+919812345674", 18_40_000, 21, "Integration support, Q3"),
    ("Anjali Menon",   "anjali@example.com", "+919812345675", 12_00_000, 19, "Training workshop"),
    ("Farhan Qureshi", "farhan@example.com", "+919812345676",  6_80_000, 17, "Monthly hosting"),
    ("Nikhil Bose",    "nikhil@example.com", "+919812345677",  3_20_000, 30, "Additional seats"),
    ("Priya Nair",     "priya@example.com",  "+919812345678",  1_10_000, 22, "Domain renewal"),
    ("Arun Pillai",    "arun@example.com",   "+919812345679", 25_00_000, 16, "Migration, milestone 1"),
]



def main() -> None:
    execute = "--execute" in sys.argv
    env = load_env()
    cfg = (load_declines().get("receivables") or {})
    terms = int(cfg.get("payment_terms_days", 14))
    grace = int(cfg.get("grace_days", 2))
    now = int(time.time())

    print("SEED RECEIVABLES  --  real invoices on the live test account")
    print(f"  mode          {'EXECUTE' if execute else 'dry run (nothing created)'}")
    print(f"  terms         net {terms} days, {grace}-day grace")
    print(f"  overdue when  issued more than {terms + grace} days ago and unpaid")
    print()

    made = 0
    for name, email, contact, paise, age_days, what in BOOK:
        overdue_by = age_days - terms
        chased = overdue_by > grace
        print(f"  {name:<16} Rs {paise/100:>9,.0f}  issued {age_days}d ago  "
              f"{overdue_by:+d}d vs terms  "
              f"{'CHASED' if chased else 'within grace, left alone'}")
        if not execute:
            continue
        body = {
            "type": "invoice",
            "description": what,
            "date": now - age_days * DAY,
            "customer": {"name": name, "email": email, "contact": contact},
            "line_items": [{"name": what, "amount": paise,
                            "currency": "INR", "quantity": 1}],
            # Razorpay requires a FUTURE expiry, so the link stays usable. The
            # debt is overdue by its terms regardless -- which is the whole
            # reason the engine does not read this field.
            "expire_by": now + 45 * DAY,
            "sms_notify": 0, "email_notify": 0,
            "notes": {"source": "receivables-seed", "book": "demo"},
        }
        # The invoices endpoint rate-limits hard and returns 429 rather than
        # queueing, so back off and retry rather than losing the row.
        for attempt in range(6):
            try:
                r = _call("POST", "/invoices", body, env)
                print(f"        -> {r.get('id')}  {r.get('short_url')}")
                made += 1
                break
            except Exception as e:                      # noqa: BLE001
                if "429" in str(e) and attempt < 5:
                    wait = 5 * (attempt + 1)
                    print(f"        .. rate limited, waiting {wait}s")
                    time.sleep(wait)
                    continue
                print(f"        !! {str(e)[:160]}")
                break
        time.sleep(2.0)

    print()
    if execute:
        print(f"created {made} invoice(s). They are real and payable.")
        print("Run:  python -m scripts.run_dunning        to see them collected")
    else:
        print("dry run -- pass --execute to create these on the account.")
        print(f"NOTE: seeded invoices are tagged `receivables-seed`, distinct "
              f"from\n      `{RECOVERY_TAG}` which marks links WE issue and "
              f"never chase.")


if __name__ == "__main__":
    main()
