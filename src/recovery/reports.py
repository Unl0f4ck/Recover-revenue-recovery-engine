"""Daily reconciliation and recovery analytics.

Two things, kept in one module because they read the same ledgers and an
operator wants them on the same page.

THE RECONCILIATION REPORT. recoup runs a "daily job" cross-checking its books
against the gateway's. We reconcile on demand, which means the answer to "did
anything come in overnight" is a command someone has to remember to run. A
dated report -- recovered since yesterday, still open, newly escalated, drifted
-- is the artefact an operator actually reads, and it is the one that surfaces
DRIFT: a link the gateway says was paid that our ledger still shows as
delivered. That gap is the whole reason to reconcile rather than assume, and
until it is reported nobody looks for it.

THE ANALYTICS. emp-billing exposes MRR, churn and cohorts; dunlo tracks
recovered revenue. Neither is the right metric for a recovery engine. What
tells an operator whether the SCHEDULE is right is recovery broken down by
decline class and by attempt number: if attempt 3 never recovers anything, the
ladder is one rung too long and every one of those contacts is spend and
annoyance for nothing.

The console shows a funnel for one moment. This is the same data over time, and
it is honest about being thin -- with one recovered campaign the per-class rates
are anecdotes, and `sample_warning` says so rather than printing 100%.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import ledger as L
from . import notify, review, view


@dataclass
class Drift:
    """The gateway and our ledger disagree. Always worth a human."""
    reference: str
    artefact: str
    ledger_says: str
    gateway_says: str
    amount_paise: int


@dataclass
class DailyReport:
    at: datetime
    since: datetime
    opened: int = 0
    contacts: int = 0
    recovered_count: int = 0
    recovered_paise: int = 0
    closed: int = 0
    escalated: int = 0
    still_open: int = 0
    at_risk_open_paise: int = 0
    deferred: int = 0
    suppressed: int = 0
    drift: list[Drift] = field(default_factory=list)
    stop_reasons: list[tuple] = field(default_factory=list)
    notification_states: dict = field(default_factory=dict)
    review: dict = field(default_factory=dict)


def _since(rows: list[dict], since: datetime, event: str) -> list[dict]:
    return [e for e in rows
            if e["event"] == event and datetime.fromisoformat(e["at"]) >= since]


def daily(now: datetime, since: datetime | None = None,
          cfg: dict | None = None, path: Path | None = None,
          notif_path: Path | None = None, review_path: Path | None = None,
          gateway=None, check_drift: bool = False) -> DailyReport:
    """What happened in the last day, and what disagrees with the gateway."""
    since = since or (now - timedelta(days=1))
    rows = L.read(path)
    campaigns = view.campaigns(now, cfg, path)

    stops: dict[str, int] = defaultdict(int)
    for e in _since(rows, since, L.SEQUENCE_STOPPED):
        stops[(e.get("extra") or {}).get("stop_reason", "unknown")] += 1

    won = _since(rows, since, L.ATTEMPT_SUCCEEDED)
    rep = DailyReport(
        at=now, since=since,
        opened=len(_since(rows, since, L.SEQUENCE_OPENED)),
        contacts=len(_since(rows, since, L.ATTEMPT_INFLIGHT)),
        recovered_count=len(won),
        recovered_paise=sum(int(e.get("amount_paise", 0)) for e in won),
        closed=len(_since(rows, since, L.SEQUENCE_STOPPED)),
        deferred=len(_since(rows, since, L.CONTACT_DEFERRED)),
        escalated=sum(1 for c in campaigns if c.state == view.ESCALATED),
        still_open=sum(1 for c in campaigns if not c.terminal),
        at_risk_open_paise=sum(c.at_risk_paise for c in campaigns
                               if not c.terminal),
        stop_reasons=sorted(stops.items(), key=lambda kv: -kv[1]),
        notification_states=notify.summary(notif_path),
        review=review.summary(now, cfg, path, review_path),
    )
    rep.suppressed = rep.notification_states.get(notify.SUPPRESSED, 0)

    if check_drift:
        rep.drift = find_drift(cfg, path, gateway)
    return rep


def find_drift(cfg: dict | None = None, path: Path | None = None,
               gateway=None) -> list[Drift]:
    """Artefacts the gateway and our ledger disagree about.

    This is what a reconciliation job is FOR. Everything else in the report can
    be computed from our own records; only this needs the gateway, and only this
    can catch the case that actually loses money -- a link that was paid while
    our ledger still shows it merely delivered, because the reconcile pass
    failed, or the process died between the read and the write.
    """
    from ..gateways import get_gateway
    gw = gateway or get_gateway("razorpay")

    out: list[Drift] = []
    seen: set[str] = set()
    for e in L.read(path):
        art = e.get("execution_reference")
        ref = e.get("reference")
        if not art or art in seen:
            continue
        seen.add(art)
        try:
            got = gw.fetch_recovery(art)
        except Exception:                                # noqa: BLE001
            continue
        ours_won = L.has_succeeded(ref, path)
        if got.paid and not ours_won:
            out.append(Drift(reference=ref, artefact=art,
                             ledger_says="not recovered",
                             gateway_says=got.status,
                             amount_paise=got.amount_paid_paise or got.amount_paise))
        elif ours_won and not got.paid:
            out.append(Drift(reference=ref, artefact=art,
                             ledger_says="recovered",
                             gateway_says=got.status,
                             amount_paise=got.amount_paise))
    return out


# ---------------------------------------------------------------------------
# analytics
# ---------------------------------------------------------------------------

MIN_SAMPLE = 20


@dataclass
class Breakdown:
    key: str
    campaigns: int
    contacts: int
    recovered: int
    recovered_paise: int
    at_risk_paise: int

    @property
    def rate(self) -> float:
        return self.recovered / self.campaigns if self.campaigns else 0.0

    @property
    def value_rate(self) -> float:
        return (self.recovered_paise / self.at_risk_paise
                if self.at_risk_paise else 0.0)

    def sample_warning(self) -> str:
        """Say when a rate is an anecdote.

        A 100% recovery rate over one campaign is not a recovery rate, and
        printing it without this line is how a demo number becomes a claim.
        """
        return ("" if self.campaigns >= MIN_SAMPLE else
                f"n={self.campaigns}; too few to read as a rate")


def by_decline_class(now: datetime, cfg: dict | None = None,
                     path: Path | None = None) -> list[Breakdown]:
    out: dict[str, dict] = defaultdict(
        lambda: {"c": 0, "won": 0, "won_p": 0, "risk": 0, "contacts": 0})
    for c in view.campaigns(now, cfg, path):
        b = out[c.invariants.decline_class if c.invariants else "?"]
        b["c"] += 1
        b["risk"] += c.at_risk_paise
        b["won"] += 1 if c.state == view.RECOVERED else 0
        b["won_p"] += c.recovered_paise
    for e in L.read(path):
        if e["event"] == L.ATTEMPT_INFLIGHT:
            out[e.get("decline_class") or "?"]["contacts"] += 1
    return sorted(
        (Breakdown(k, v["c"], v["contacts"], v["won"], v["won_p"], v["risk"])
         for k, v in out.items()), key=lambda b: -b.at_risk_paise)


def by_attempt(now: datetime, path: Path | None = None) -> list[dict]:
    """Which rung of the ladder actually recovers anything.

    The single most actionable number a dunning system produces. If attempt 3
    recovers nothing across a real sample, the ladder is a rung too long and
    every one of those contacts is spend, annoyance and reputation for nothing.
    """
    sent: dict[int, int] = defaultdict(int)
    won: dict[int, int] = defaultdict(int)
    won_p: dict[int, int] = defaultdict(int)
    for e in L.read(path):
        if e["event"] == L.ATTEMPT_INFLIGHT:
            sent[int(e["attempt_no"])] += 1
        elif e["event"] == L.ATTEMPT_SUCCEEDED:
            won[int(e["attempt_no"])] += 1
            won_p[int(e["attempt_no"])] += int(e.get("amount_paise", 0))
    rows = []
    for n in sorted(set(sent) | set(won)):
        rows.append({"attempt": n + 1, "contacts": sent[n],
                     "recovered": won[n], "recovered_paise": won_p[n],
                     "rate": (won[n] / sent[n]) if sent[n] else 0.0,
                     "thin": sent[n] < MIN_SAMPLE})
    return rows


def over_time(now: datetime, days: int = 14, path: Path | None = None
              ) -> list[dict]:
    """Contacts and recoveries per day. Thin by construction on a young ledger."""
    rows = L.read(path)
    buckets: dict[str, dict] = defaultdict(
        lambda: {"contacts": 0, "recovered": 0, "recovered_paise": 0,
                 "opened": 0})
    for e in rows:
        day = e["at"][:10]
        if e["event"] == L.ATTEMPT_INFLIGHT:
            buckets[day]["contacts"] += 1
        elif e["event"] == L.ATTEMPT_SUCCEEDED:
            buckets[day]["recovered"] += 1
            buckets[day]["recovered_paise"] += int(e.get("amount_paise", 0))
        elif e["event"] == L.SEQUENCE_OPENED:
            buckets[day]["opened"] += 1
    cutoff = (now - timedelta(days=days)).date().isoformat()
    return [{"day": d, **v} for d, v in sorted(buckets.items()) if d >= cutoff]
