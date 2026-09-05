"""The payment-degradation workflow, actually wired to live traffic.

Until now this workflow reported "BLOCKED: needs 30 payments in one segment",
which reads as broken and is the wrong thing to say. Insufficient volume is not
a blocker; it is a RESULT. A monitoring loop that runs, looks, and finds nothing
is working exactly as intended -- the alternative, a loop that fires on four
payments, is the false-intervention defect this project already found and fixed
once.

So this runs the real loop on the real account, every time, and reports what it
concluded:

    DETECT     bucket live payments into 5-minute cells, run the binomial CUSUM
    DIAGNOSE   build cell evidence, run the L1/L2 attribution ladder
    DECIDE     map the mechanism family to an action through the frozen policy
    REPORT     an incident with a cause and an action, or "looked, found nothing"

The detector and the ladder here are the SAME code the 240-scenario frozen
experiment scored. They were never simulator-specific; `to_windows` is the seam
that lets identical machinery read the API. What differs on live data is the
baseline, and that is stated below rather than glossed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from ..attribution.evidence import CellEvidence, IncidentEvidence
from ..attribution.ladder import diagnose
from ..detector.cusum import Alert, run_cusum
from ..eval.bootstrap import wilson_interval
from ..ingest.razorpay_source import RealPayment, to_windows
from ..policy import decide, load_policy
from ..schema import Action, Diagnosis

IST = timezone(timedelta(hours=5, minutes=30))

# Frozen detector settings, from config/eval.yaml. Not re-tuned here: the whole
# point of the frozen experiment was that these were chosen before the data was
# seen, and picking new ones for live traffic would throw that away.
H = 5.5
KAPPA = 1.6


@dataclass
class CellScan:
    """One segment/method cell, as the detector saw it."""
    segment: str
    method: str
    windows: int
    attempts: int
    failures: int
    rate: float
    at_risk_paise: int = 0
    alerts: list[Alert] = field(default_factory=list)

    @property
    def alerting(self) -> bool:
        return bool(self.alerts)


@dataclass
class DegradationScan:
    """What the loop concluded this pass. A finding of nothing is a finding."""
    ran_at: datetime
    payments_seen: int
    cells: list[CellScan] = field(default_factory=list)
    baseline_rate: float = 0.0
    incident: bool = False
    diagnosis: Diagnosis | None = None
    action: Action | None = None
    at_risk_paise: int = 0
    conclusion: str = ""
    thin: bool = False               # too little traffic to read the result
    min_volume: int = 30

    @property
    def alerting_cells(self) -> list[CellScan]:
        return [c for c in self.cells if c.alerting]

    @property
    def largest_cell(self) -> int:
        return max((c.attempts for c in self.cells), default=0)


def _p0(payments: list[RealPayment]) -> float:
    """The baseline failure rate the CUSUM measures departures from.

    In the frozen experiment this came from a seasonal binomial GLM fitted over
    a warm-up period. A live test account has no warm-up and no seasonality
    worth fitting -- a few dozen payments across a couple of days -- so the
    baseline here is the POOLED failure rate across all cells.

    That is a weaker baseline and it is stated rather than hidden. It means the
    detector on live data is asking "is this cell failing more than the account
    as a whole", which is the right question when there is no history, and a
    strictly easier one than the seasonal version. It also means a uniformly bad
    account looks calm -- correctly, because a rail that has always been broken
    is not a degradation, it is a configuration problem.
    """
    n = len(payments)
    if not n:
        return 0.0
    return sum(1 for p in payments if p.failed) / n


def scan(payments: list[RealPayment], now: datetime | None = None,
         min_volume: int = 30, cfg: dict | None = None) -> DegradationScan:
    """Run detect -> diagnose -> decide over live payments. Always runs."""
    now = now or datetime.now(IST)
    res = DegradationScan(ran_at=now, payments_seen=len(payments),
                          min_volume=min_volume)
    if not payments:
        res.conclusion = "no payments on the account to look at"
        res.thin = True
        return res

    p0 = _p0(payments)
    res.baseline_rate = p0
    cells = to_windows(payments)

    for (segment, method), windows in sorted(cells.items()):
        # OBSERVED windows only, in time order. A five-minute window with no
        # traffic carries no evidence either way, so omitting it is equivalent
        # to including it with n=0 -- and filling the gaps would mean thousands
        # of empty rows across a few days of a quiet test account.
        idx = sorted(windows)
        x = np.array([sum((windows[i].get("failures_by_source") or {}).values())
                      for i in idx], dtype=float)
        n = np.array([windows[i]["n_attempts"] for i in idx], dtype=float)
        cell = CellScan(segment=segment, method=method, windows=len(idx),
                        attempts=int(n.sum()), failures=int(x.sum()),
                        rate=float(x.sum() / n.sum()) if n.sum() else 0.0,
                        at_risk_paise=sum(
                            windows[i].get("amount_at_risk_paise", 0)
                            for i in idx))
        if n.sum() >= 2 and p0 > 0:
            cell.alerts = run_cusum(x, n, np.full(len(idx), p0), H,
                                    kappa=KAPPA)
        res.cells.append(cell)

    res.thin = res.largest_cell < min_volume

    alerting = res.alerting_cells
    if not alerting:
        res.conclusion = (
            f"looked at {len(payments)} payments across {len(res.cells)} "
            f"segment/method cells; no cell is failing more than the account "
            f"baseline of {p0:.0%}. Nothing to fix.")
        return res

    # ---- DIAGNOSE ---------------------------------------------------------
    # Build the same evidence structure the frozen experiment fed the ladder.
    ev_cells: list[CellEvidence] = []
    for c in res.cells:
        lo, hi = wilson_interval(c.failures, max(c.attempts, 1))
        shift = c.rate - p0
        ev_cells.append(CellEvidence(
            issuer=c.segment, method=c.method, n_attempts=c.attempts,
            failures=c.failures, baseline_p=p0, shift=shift,
            shift_lo=lo - p0, shift_hi=hi - p0,
            z=_z(c.failures, c.attempts, p0), alerting=c.alerting))

    # THE SIGNATURE COMES FROM THE INCIDENT, NOT FROM THE BOOK.
    #
    # This took the mode over EVERY failed payment on the account, which means
    # it described the background rather than the thing being diagnosed. On a
    # book failing at a healthy 9% with one cell blown out to 62%, ordinary
    # customer declines outnumber the incident's failures roughly eight to one,
    # so `dominant_source` came back "customer" while the alerting cell was
    # 100% `issuer_bank`. The ladder then found the right cause node,
    # `mechanism_family` resolved to None because no family matches
    # customer/authorization for an issuer outage, and `decide` returned
    # NO_ACTION. Detection worked, localisation worked, and the loop did
    # nothing -- the most expensive kind of quiet failure, because every
    # visible intermediate step looked right.
    alerting_cells = {(c.segment, c.method) for c in alerting}
    incident_failures = [p for p in payments
                         if p.failed and (p.issuer, p.method) in alerting_cells]
    # Fall back to the whole book only if the alerting cells somehow carry no
    # attributed failures at all; a wrong-but-stated source beats a crash.
    failed = incident_failures or [p for p in payments if p.failed]
    src = _mode([p.error_source for p in failed if p.error_source]) or "issuer_bank"
    step = _mode([p.error_step for p in failed if p.error_step])
    methods = sorted({c.method for c in alerting})

    ev = IncidentEvidence(
        incident_id=f"live-{now:%Y%m%dT%H%M}",
        dominant_source=src, dominant_step=step, methods=methods,
        cells=ev_cells)

    res.incident = True
    res.diagnosis = diagnose(ev)
    res.action = decide(res.diagnosis, cfg or load_policy())
    res.at_risk_paise = sum(c.at_risk_paise for c in alerting)

    cause = res.diagnosis.cause_node or "no single cause"
    res.conclusion = (
        f"{len(alerting)} cell(s) failing above the {p0:.0%} baseline; "
        f"diagnosed at {res.diagnosis.level_used} as {cause} "
        f"({res.diagnosis.mechanism_family or 'unknown mechanism'}); "
        f"the policy says {res.action.value}")
    return res


def _z(failures: int, attempts: int, p0: float) -> float:
    if attempts <= 0 or p0 <= 0 or p0 >= 1:
        return 0.0
    se = (p0 * (1 - p0) / attempts) ** 0.5
    return (failures / attempts - p0) / se if se else 0.0


def _mode(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)


def summarise(s: DegradationScan) -> dict:
    """What the console shows for this workflow."""
    return {
        "ran_at": s.ran_at.isoformat(),
        "payments_seen": s.payments_seen,
        "cells": len(s.cells),
        "largest_cell": s.largest_cell,
        "baseline_rate": s.baseline_rate,
        "alerting": [f"{c.segment}/{c.method}" for c in s.alerting_cells],
        "incident": s.incident,
        "cause": (s.diagnosis.cause_node if s.diagnosis else None),
        "mechanism": (s.diagnosis.mechanism_family if s.diagnosis else None),
        "level": (s.diagnosis.level_used if s.diagnosis else None),
        "action": (s.action.value if s.action else None),
        "at_risk_paise": s.at_risk_paise,
        "conclusion": s.conclusion,
        "thin": s.thin,
        "min_volume": s.min_volume,
        "thin_note": (
            f"the busiest cell has {s.largest_cell} payments; below about "
            f"{s.min_volume} a rate shift cannot be told from noise, so a "
            f"quiet result here means 'not enough traffic to say', not "
            f"'definitely fine'" if s.thin else ""),
    }
