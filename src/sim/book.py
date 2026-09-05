"""A synthetic book of revenue at risk.

Produces the same `RevenueAtRisk` objects `src/ingest/` produces from the live
Razorpay account, so `campaign.run_pass` cannot tell where they came from.

EVERY IDENTIFIER IS OBVIOUSLY FAKE, and that is a safety property rather than a
stylistic one. References are prefixed `sim_`, and contacts are addresses at
`.invalid` -- the TLD RFC 2606 reserves precisely so it can never resolve. If a
simulated case ever leaked into a path that actually sends, there is no inbox
at the other end and no phone number that belongs to anyone. The alternative,
plausible-looking addresses at a real domain, is one configuration mistake away
from messaging strangers.

THE DECLINE REASONS ARE NOT INVENTED EITHER. They are drawn from the class
tables in `config/declines.yaml`, which are Razorpay's own published strings.
So a generated failure classifies through the same `declines.classify` the live
ingest uses, and a class the taxonomy does not know would show up here as
UNKNOWN rather than being quietly assigned.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from src.ingest.razorpay_source import RevenueAtRisk
from src.recovery.declines import load_declines

# The class mix for failed payments. Shared with the recovery-curve analysis
# rather than re-chosen here -- see src/sim/customer.py for why the two must
# not drift apart.
from scripts.recovery_curve import BOOK_A, BOOK_B

# Stream composition. A merchant's book is not one problem: most of the volume
# is checkout traffic that failed or was abandoned, and a smaller number of
# much larger invoices sit behind it. The amounts matter more than the counts,
# which is why the report breaks out both.
STREAMS = {
    "payment_failure":     0.40,
    "checkout_abandoned":  0.31,
    "overdue_receivable":  0.17,
    "subscription_failure": 0.12,
}

# Amounts, lognormal. Medians chosen so each stream straddles its own exposure
# floor from `economics.py` -- a book where nothing is ever declined would hide
# the floor rather than demonstrate it.
AMOUNTS = {                       # (median rupees, sigma)
    "payment_failure":     (3_500, 1.05),
    "checkout_abandoned":  (2_400, 1.15),
    "overdue_receivable":  (34_000, 0.95),
    # One CYCLE, not the lifetime value. A monthly plan, tightly distributed --
    # subscription pricing comes from a short list of plans, not from whatever
    # happened to be in a basket. The remaining cycles are carried in `detail`
    # so the console can show what halting actually costs without the exposure
    # floor being applied to a projection.
    "subscription_failure": (1_400, 0.55),
}

ISSUERS = ["HDFC", "ICICI", "SBIN", "AXIS", "KOTAK", "IDFC", "YESB", "PUNB"]
UPI_HANDLES = ["okhdfcbank", "ybl", "paytm", "okaxis", "apl", "ibl"]
METHODS = {
    "payment_failure":    ["card", "card", "upi", "upi", "netbanking", "wallet"],
    "checkout_abandoned": ["card", "upi", "upi", "netbanking"],
    "overdue_receivable": ["netbanking", "card", "upi"],
    # A mandate lives on a card or a UPI Autopay handle. Netbanking has no
    # standing-instruction equivalent here, so it does not appear.
    "subscription_failure": ["card", "card", "card", "upi"],
}

FIRST = ["rajat", "priya", "aditya", "meera", "farhan", "kavya", "ishan",
         "ananya", "vikram", "sneha", "arjun", "divya", "rohit", "nisha",
         "karthik", "pooja", "sameer", "tara", "manav", "ritu"]
LAST = ["sharma", "iyer", "khan", "reddy", "patel", "bose", "nair", "gupta",
        "menon", "shah", "verma", "das", "rao", "joshi", "chawla"]


def _reasons_by_class(cfg: dict) -> dict[str, list[str]]:
    return {name: list(spec.get("reasons") or [])
            for name, spec in cfg["classes"].items()}


def _amount(stream: str, rng) -> int:
    median, sigma = AMOUNTS[stream]
    rupees = float(rng.lognormal(np.log(median), sigma))
    return int(round(min(rupees, 400_000)) * 100)


def _contact(rng, i: int) -> str:
    """An address that cannot exist. See the module docstring."""
    if rng.random() < 0.22:
        # A phone number in the reserved 99999 test range, so the suppression
        # path is exercised on both shapes of identity, not just e-mail.
        return f"+9199999{i:05d}"
    return (f"{FIRST[rng.integers(len(FIRST))]}."
            f"{LAST[rng.integers(len(LAST))]}{i}@sim.invalid")


def generate(n: int = 240, now: datetime | None = None, seed: int = 20260829,
             cfg: dict | None = None, arrival_days: float = 0.0
             ) -> list[RevenueAtRisk]:
    """One synthetic batch, reproducible from the seed alone.

    `arrival_days` SPREADS THE BOOK OVER TIME, and getting this wrong quietly
    disabled a compliance control. With everything dated before `now`, every
    sequence opens on the first tick, so every ladder is phase-locked to the
    same hour -- and since the schedule offsets are 1, 6, 24, 72 and 168 hours,
    every single rung then falls at almost the same time of day. Open a batch
    at 09:00 and nothing is ever due at 03:00, so the quiet-hours guard reports
    zero deferrals and looks either unnecessary or broken. It is neither; it
    was never asked.

    Real failures arrive continuously, so failures and abandonments are spread
    across the run window as well as before it. Receivables are the exception:
    an invoice has to pass its due date to be overdue, so that stream is always
    aged and always present at the start.
    """
    cfg = cfg or load_declines()
    rng = np.random.default_rng(seed)
    now = now or datetime.now()
    reasons = _reasons_by_class(cfg)

    streams = list(STREAMS)
    weights = np.array([STREAMS[s] for s in streams], dtype=float)
    counts = rng.multinomial(n, weights / weights.sum())

    classes = [c for c in BOOK_A if BOOK_A[c] > 0]
    cls_w = np.array([BOOK_A[c] for c in classes], dtype=float)
    cls_w = cls_w / cls_w.sum()

    out: list[RevenueAtRisk] = []
    i = 0
    for stream, k in zip(streams, counts):
        for _ in range(int(k)):
            i += 1
            method = METHODS[stream][rng.integers(len(METHODS[stream]))]
            if method == "upi":
                segment = UPI_HANDLES[rng.integers(len(UPI_HANDLES))].upper()
            elif method == "wallet":
                segment = "WALLET"
            else:
                segment = ISSUERS[rng.integers(len(ISSUERS))]

            detail: dict = {"customer_ref": _contact(rng, i), "synthetic": True}
            if stream == "payment_failure":
                cls = classes[int(rng.choice(len(classes), p=cls_w))]
                pool = reasons.get(cls) or ["payment_failed"]
                detail["error_reason"] = pool[rng.integers(len(pool))]
                prefix = "sim_pay"
                age_days = float(rng.uniform(-arrival_days, 6))
            elif stream == "checkout_abandoned":
                prefix = "sim_order"
                age_days = float(rng.uniform(-arrival_days, 10))
            elif stream == "subscription_failure":
                # BOOK_B, not BOOK_A. A subscription book's decline mix is not
                # a checkout book's: expired cards and insufficient funds
                # dominate, interactive auth barely appears because a recurring
                # charge skips it, and there is no checkout to abandon. Using
                # the checkout mix here would have quietly imported the wrong
                # ceiling -- see docs/RECOVERY_RATE.md, where that distinction
                # is the whole reason the published 70-80% band is a
                # subscription number.
                bcls = [c for c in BOOK_B if BOOK_B[c] > 0]
                bw = np.array([BOOK_B[c] for c in bcls], dtype=float)
                cls = bcls[int(rng.choice(len(bcls), p=bw / bw.sum()))]
                pool = reasons.get(cls) or ["payment_failed"]
                detail["error_reason"] = pool[rng.integers(len(pool))]
                # THE MANDATE LIVES HERE, on the case. Not every subscription
                # has a usable one -- an expired card is exactly a mandate that
                # has stopped working -- so a slice of the book is deliberately
                # without, and those get the contacting rungs only.
                detail["mandate"] = bool(rng.random() < 0.88)
                detail["status"] = "halted" if rng.random() < 0.45 else "pending"
                detail["paid_count"] = int(rng.integers(1, 24))
                detail["remaining_count"] = int(rng.integers(1, 18))
                prefix = "sim_sub"
                age_days = float(rng.uniform(-arrival_days, 8))
            else:
                # Receivables are found overdue, not found failing -- an
                # invoice has to pass its due date first, so this stream is
                # systematically older than the other two.
                prefix, age_days = "sim_inv", float(rng.uniform(3, 45))
                detail["due_at"] = (now - timedelta(days=age_days)).isoformat()

            out.append(RevenueAtRisk(
                kind=stream,
                reference=f"{prefix}_{i:04d}{_suffix(rng)}",
                created_at=now - timedelta(days=age_days),
                amount_paise=_amount(stream, rng),
                segment=segment, method=method, detail=detail))

    out.sort(key=lambda r: r.created_at)
    return out


def _suffix(rng) -> str:
    """A Razorpay-shaped tail, so references look like ids rather than counters
    in the console -- without ever colliding with a real one, because no real
    Razorpay id begins `sim_`."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
    return "".join(alphabet[rng.integers(len(alphabet))] for _ in range(6))
