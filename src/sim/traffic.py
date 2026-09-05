"""A payment stream with a real incident buried in it.

WHY THIS EXISTS. The payment-degradation loop is the only one of the four that
cannot be demonstrated on this account at all, and the reason is not a missing
feature — it is arithmetic. The detector is a binomial CUSUM over 5-minute
windows per segment, and it needs roughly thirty attempts in a cell before a
rate shift is distinguishable from noise. The live test account's busiest cell
has one. So the loop runs on every pass, correctly reports "looked, found
nothing", and there is no honest way to make it find something.

WHAT IS SIMULATED HERE IS THE TRAFFIC, AND ONLY THE TRAFFIC. This module emits
`RealPayment` objects — the same objects `razorpay_source` builds from the live
API — and hands them to `degradation.scan` unchanged. The detector, the
attribution ladder, the policy and the frozen thresholds (h=5.5, kappa=1.6) are
all production code and none of them knows the traffic was generated.

THE INCIDENT IS NOT ANNOUNCED. The scan is not told which segment is
degrading, when it starts, or that anything is wrong at all. It is given a
stream and has to find the cell itself. That is what makes the output worth
looking at: `injected()` returns the ground truth so a caller can check the
answer AFTER the fact, and `scan` never sees it. A generator that told the
detector where to look would be testing nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from src.ingest.razorpay_source import RealPayment

IST = timezone(timedelta(hours=5, minutes=30))

# A healthy Indian payments book does not run at 100%. Somewhere around one
# attempt in twelve fails on an ordinary day, and the detector's whole job is
# to tell that steady state apart from something going wrong.
BASELINE_FAILURE = 0.085

ISSUERS = ["HDFC", "ICICI", "SBIN", "AXIS", "KOTAK", "IDFC"]
UPI_HANDLES = ["OKHDFCBANK", "YBL", "PAYTM", "OKAXIS"]
METHODS = ["card", "card", "card", "upi", "upi", "netbanking"]

# Error signatures by where the failure actually happened. These drive the
# attribution ladder's `dominant_source` and `dominant_step`, so they have to
# be consistent with the mechanism being simulated rather than sampled at
# random -- an issuer outage that reported `gateway_technical_error` would be
# asking the ladder to diagnose a contradiction.
SIGNATURES = {
    "issuer_bank": [
        ("BAD_REQUEST_ERROR", "authentication", "bank_technical_error"),
        ("BAD_REQUEST_ERROR", "authorization", "issuer_technical_error"),
        ("BAD_REQUEST_ERROR", "authorization", "bank_not_available"),
    ],
    "gateway": [
        ("GATEWAY_ERROR", "payment_initiation", "gateway_technical_error"),
        ("GATEWAY_ERROR", "authorization", "payment_timed_out"),
    ],
    "customer": [
        ("BAD_REQUEST_ERROR", "authorization", "insufficient_funds"),
        ("BAD_REQUEST_ERROR", "authentication", "incorrect_otp"),
    ],
}


@dataclass(frozen=True)
class Injected:
    """The ground truth. Never shown to the detector."""
    segment: str
    method: str
    started_at: datetime
    ends_at: datetime
    failure_rate: float
    source: str
    attempts: int

    def describe(self) -> str:
        return (f"{self.segment}/{self.method} failing at "
                f"{self.failure_rate:.0%} from {self.started_at:%H:%M} to "
                f"{self.ends_at:%H:%M} ({self.source})")


def _sig(source: str, rng) -> tuple[str, str, str]:
    pool = SIGNATURES.get(source) or SIGNATURES["customer"]
    return pool[int(rng.integers(len(pool)))]


def generate(minutes: int = 240, per_minute: float = 9.0,
             seed: int = 7, now: datetime | None = None,
             incident: bool = True, incident_minutes: int = 45,
             incident_rate: float = 0.62,
             incident_source: str = "issuer_bank"
             ) -> tuple[list[RealPayment], Injected | None]:
    """A stream of payments, and the incident hidden in it.

    Returns the ground truth alongside so a caller can grade the detector, but
    the payments themselves carry nothing the detector could use as a hint: an
    incident payment is an ordinary failed payment with an ordinary error
    signature, and the only thing that marks the window is that there are more
    of them.
    """
    rng = np.random.default_rng(seed)
    now = now or datetime.now(IST)
    start = now - timedelta(minutes=minutes)

    seg_method = [(i, "card") for i in ISSUERS] + \
                 [(h, "upi") for h in UPI_HANDLES] + \
                 [(i, "netbanking") for i in ISSUERS[:3]]

    hit = seg_method[int(rng.integers(len(seg_method)))] if incident else None
    # Placed late enough that the detector has a run of healthy windows to form
    # a baseline from, and ends before `now` so the whole episode is inside the
    # data rather than still unfolding at the edge.
    inc_start = start + timedelta(
        minutes=int(minutes * 0.55)) if incident else None
    inc_end = (inc_start + timedelta(minutes=incident_minutes)
               if incident else None)

    out: list[RealPayment] = []
    inc_attempts = 0
    n = 0
    for m in range(minutes):
        at_minute = start + timedelta(minutes=m)
        k = int(rng.poisson(per_minute))
        for _ in range(k):
            n += 1
            seg, method = seg_method[int(rng.integers(len(seg_method)))]
            at = at_minute + timedelta(seconds=float(rng.uniform(0, 60)))

            in_incident = (incident and hit is not None
                           and (seg, method) == hit
                           and inc_start <= at < inc_end)
            if in_incident:
                inc_attempts += 1
                p_fail, source = incident_rate, incident_source
            else:
                p_fail, source = BASELINE_FAILURE, "customer"

            failed = bool(rng.random() < p_fail)
            code, step, reason = _sig(source, rng) if failed else (None, None, None)

            out.append(RealPayment(
                payment_id=f"pay_sim{n:06d}",
                created_at=at,
                amount_paise=int(max(10000, rng.lognormal(np.log(2500), 0.9)) * 100),
                status="failed" if failed else "captured",
                method=method,
                bank=seg if method in ("card", "netbanking") else None,
                wallet=None,
                vpa=f"user{n}@{seg.lower()}" if method == "upi" else None,
                error_code=code,
                error_source=source if failed else None,
                error_step=step if failed else None,
                error_reason=reason,
                order_id=None,
            ))

    out.sort(key=lambda p: p.created_at)
    truth = (Injected(segment=hit[0], method=hit[1], started_at=inc_start,
                      ends_at=inc_end, failure_rate=incident_rate,
                      source=incident_source, attempts=inc_attempts)
             if incident and hit else None)
    return out, truth
