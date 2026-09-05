"""Seed the test-mode account with a realistic checkout batch.

Creates Razorpay ORDERS, which is what a real merchant's checkout does before a
customer pays. Orders left unpaid become the checkout-abandonment stream the
recovery agent ingests -- so the batch it runs on is genuine API data, not a
fixture.

Orders are free, inert and test-mode only. It does NOT create payment links
here: test mode caps links at roughly 30 per business, and the recovery agent
needs that budget to actually recover things.

Amounts follow a realistic Indian e-commerce spread so the intervention gate
has something to discriminate on -- small baskets that are not worth chasing,
large ones that clearly are.

Usage:  python -m scripts.seed_account [n]
"""
from __future__ import annotations

import sys
import time

import numpy as np

from src.execution.razorpay import _call, load_env

# Rs 400 to Rs 60,000. Lognormal, so most baskets are small and a few are large
# -- which is what makes an exposure floor a meaningful decision rather than a
# formality.
LOG_MU, LOG_SIGMA = np.log(350000.0), 1.15
FLOOR, CEIL = 40000, 6_000_000


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    env = load_env()
    rng = np.random.default_rng(20260827)

    created, total = [], 0
    for i in range(n):
        amt = int(np.clip(rng.lognormal(LOG_MU, LOG_SIGMA), FLOOR, CEIL))
        amt = amt - (amt % 100)                    # whole rupees
        body = {
            "amount": amt, "currency": "INR",
            "receipt": f"seed-{i:03d}",
            "notes": {"source": "causal-payment-recovery",
                      "purpose": "checkout-abandonment-batch"},
        }
        try:
            r = _call("POST", "/orders", body, env)
            created.append((r.get("id"), amt))
            total += amt
        except Exception as exc:                   # noqa: BLE001
            print(f"  order {i} failed: {exc}")
            break
        time.sleep(0.12)

    print(f"created {len(created)} orders, Rs {total/100:,.0f} total")
    amts = sorted(a for _, a in created)
    if amts:
        print(f"  smallest Rs {amts[0]/100:,.0f}   median Rs "
              f"{amts[len(amts)//2]/100:,.0f}   largest Rs {amts[-1]/100:,.0f}")
        print(f"  above the Rs 5,000 gate floor: "
              f"{sum(1 for a in amts if a >= 500000)}/{len(amts)}")


if __name__ == "__main__":
    main()
