"""Build the console's data file.

The console is a STATIC READ. It computes nothing: every figure it shows is
produced here, from the dunning ledger, the live account and the offline
validation. A screen therefore cannot disagree with what the system actually
did, and the demo renders with no network and no pipeline running.

The product changed shape and so did this file. It used to describe a batch of
one-shot recovery actions. It now describes CAMPAIGNS -- persistent sequences
that wait, retry, contact, reconcile and stop -- because that is what the system
is, and a console rendering the older shape would undersell it.

Usage:  python -m scripts.build_ui_data
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from scripts import recovery_curve as RC
from src.eval.bootstrap import wilson_interval
from src.ingest.razorpay_source import account_summary
from src.gateways import get_gateway
from src.recovery import ledger as L
from src.recovery import notify, reports, review, suppression
from src.recovery import promises, view as V
from src.recovery import channels as CH
from src.recovery import workflows as WF
from src.recovery import degradation as DEG
from src.recovery import loopcheck as LOOPCHECK
from src.recovery.graph import build as build_graph
from src.recovery import webhooks as WH
from src.recovery.campaign import IST, run_pass
from src.recovery.declines import load_declines
from src.recovery.runner import collect

OUT = Path("ui/data.js")
LEDGER = Path("data/live/dunning_ledger.jsonl")
# The subscription book has its own merchant, its own gateway and its own
# ledger (config/merchants.yaml). The console shows all four loops, so it reads
# both -- a stream that exists but is invisible here is the same problem, in a
# different place, as a stream reported blocked when it is running.
SUBS_LEDGER = Path("data/live/acme_subs.dunning_ledger.jsonl")
PROJ = Path("data/live/dunning_ledger.projection.jsonl")
# The projection gets its OWN notification and suppression ledgers. Sharing
# the live ones let a projection write contact records that the NEXT build
# then read as real history -- every rung looked already-sent, so no
# sequence advanced and the 14-day picture froze on day zero.
PROJ_NOTIF = Path("data/live/notifications.projection.jsonl")
PROJ_SUP = Path("data/live/suppression.projection.jsonl")
PROMISES = Path("data/live/promises.jsonl")
NOTIF = Path("data/live/notifications.jsonl")
REVIEW = Path("data/live/review.jsonl")
SUPPRESS = Path("data/live/suppression.jsonl")
WEBHOOKS = Path("data/live/webhook_events.jsonl")
FROZEN = Path("data/frozen")


def _enc(o):
    return o.isoformat() if isinstance(o, datetime) else o


def campaigns_block(now: datetime, cfg: dict) -> dict:
    cs = V.campaigns(now, cfg, LEDGER)
    if SUBS_LEDGER.exists():
        cs = cs + V.campaigns(now, cfg, SUBS_LEDGER)
    return {
        "campaigns": [
            {**asdict(c),
             "opened_at": c.opened_at.isoformat(),
             "failed_at": c.failed_at.isoformat() if c.failed_at else None,
             "terminal": c.terminal,
             "timeline": [{**asdict(t), "at": t.at.isoformat()}
                          for t in c.timeline],
             "invariants": {k: _enc(v)
                            for k, v in asdict(c.invariants).items()}}
            for c in cs],
        "states": V.state_counts(cs),
        "funnel": V.funnel(cs, LEDGER),
        "at_risk_paise": sum(c.at_risk_paise for c in cs),
        "recovered_paise": L.recovered_paise(LEDGER),
        "n_open": sum(1 for c in cs if not c.terminal),
        "n_terminal": sum(1 for c in cs if c.terminal),
    }


def projection_block(now: datetime, cfg: dict, horizon_days: int = 14) -> dict:
    """Where these same campaigns end up if nobody pays.

    Run on a SCRATCH ledger against the real items at risk, through the real
    state machine at the real cadence. Nothing is invented -- only the clock
    moves. It is labelled a projection everywhere it is shown, and it exists
    because the live ledger is hours old: the states that matter most for
    judging a sequencer (escalated, written off, exhausted) are days away, and a
    console that only ever shows WAITING cannot demonstrate that the thing
    terminates.
    """
    for f in (PROJ, PROJ_NOTIF, PROJ_SUP):
        f.unlink(missing_ok=True)
    items = collect()
    snap_days = [1, 3, 7, 14]
    snapshots = []
    # Sample at MIDDAY, not at 24-hour multiples of the start time. This run
    # begins in the small hours, so day-boundary snapshots all land inside
    # quiet hours and every campaign reads as QUIET_HOLD -- an artefact of when
    # the batch happened to start, rendered as if it were the system's steady
    # state. Midday is the hour an operator would actually look.
    noon_offset = (12 - now.hour) % 24
    snap_hours = {noon_offset + 24 * (d - 1): d for d in snap_days}
    for h in range(0, horizon_days * 24 + 1):
        t = now + timedelta(hours=h)
        run_pass(items, t, cfg, dry_run=True, path=PROJ,
                 notif_path=PROJ_NOTIF, sup_path=PROJ_SUP)
        if h in snap_hours:
            snapshots.append({"day": snap_hours[h],
                              "states": V.state_counts(V.campaigns(t, cfg, PROJ))})
    cs = V.campaigns(now + timedelta(days=horizon_days), cfg, PROJ)
    rows = L.read(PROJ)
    contacting = set(cfg["compliance"]["contacting_channels"])
    return {
        "horizon_days": horizon_days,
        "states": V.state_counts(cs),
        "snapshots": snapshots,
        "n_attempts": sum(1 for e in rows if e["event"] == L.ATTEMPT_DELIVERED),
        # Distinct steps held, not deferral records written: an hourly pass
        # re-defers the same step every hour it stays inside quiet hours, so
        # the raw event count measures how often we looked, not how many
        # customers were spared a 3am message.
        "n_deferred": len({(e["reference"], e["attempt_no"]) for e in rows
                           if e["event"] == L.CONTACT_DEFERRED}),
        "n_contacts": sum(1 for e in rows if e["event"] == L.ATTEMPT_INFLIGHT
                          and e.get("channel") in contacting),
        "n_stopped": sum(1 for c in cs if c.terminal),
        "stop_reasons": sorted(
            {r: sum(1 for e in rows if e["event"] == L.SEQUENCE_STOPPED
                    and (e.get("extra") or {}).get("stop_reason") == r)
             for r in {(e.get("extra") or {}).get("stop_reason")
                       for e in rows if e["event"] == L.SEQUENCE_STOPPED}
             if r}.items(), key=lambda kv: -kv[1]),
    }


# The two revenue streams are two different problems and are reported apart.
#
# A failed payment announces itself and says why -- the bank hands us an error
# code, so the engine can diagnose it and pick a different rail. An abandoned
# checkout says nothing at all: no error, no code, no report, just an order that
# was never paid. They have different causes, different ladders, different
# ceilings, and merging them into one list produces a number that describes
# neither. The whole recovery-rate finding turns on the distinction.
_BUCKET = {
    "WAITING": "waiting", "DUE": "waiting", "QUIET_HOLD": "waiting",
    "DELIVERED": "chasing", "RETRY_SCHEDULED": "chasing",
    "RECOVERED": "won",
}


def streams_block(campaigns: list[dict], items: list, now: datetime,
                  cfg: dict) -> list[dict]:
    """One block per WORKFLOW, not one merged list.

    The four streams are four different problems. They are detected
    differently -- statistically, by absence, per subscription, by arithmetic on
    a date -- diagnosed differently, and chased on different ladders. Merging
    them produces a number that describes none of them, and it is exactly the
    distinction the recovery-rate finding turns on.

    Each block carries its own readiness, so a reviewer sees WHY a workflow is
    quiet rather than one global "some of this is simulated" footnote.
    """
    from src.gateways import get_gateway
    from src.ingest import subscription_source as _SS
    wf_cfg = CH.load_workflows()
    caps = get_gateway("razorpay").capabilities()

    # WHICH BOOK ANSWERS FOR EACH LOOP. Two of the four cannot be served by
    # this Razorpay account -- one starved, one gated -- so they read books
    # this project holds. The live account is tried first for both, every time,
    # and the console prints which one answered.
    # The degradation loop RUNS -- every pass -- and reports what it concluded.
    # "Looked, found nothing" is a result, not a blocker. Live traffic when it
    # can support a decision, our own book when it cannot, and the console says
    # which answered. See ingest/traffic_source.
    from src.ingest import traffic_source as TS
    _t = TS.collect()
    deg = DEG.summarise(DEG.scan(_t.payments, now))
    deg["source"] = _t.source
    deg["source_note"] = _t.note

    global _SOURCES, _CAPS_FOR
    subs = _SS.read_local()
    _SOURCES = {"payment_degradation": _t.source,
                "subscription_failure": "local" if subs else "none"}
    _CAPS_FOR = ({"subscription_failure": get_gateway("local").capabilities()}
                 if subs else {})
    items = list(items) + subs
    opened = {c["reference"] for c in campaigns}
    out = []

    for wf in WF.ordered(wf_cfg):
        mine = [c for c in campaigns if c["kind"] == wf.kind]
        declined = [i for i in items
                    if i.kind == wf.kind and i.reference not in opened]
        buckets = {"waiting": 0, "chasing": 0, "won": 0, "closed": 0}
        bucket_paise = dict(buckets)
        for c in mine:
            if c["state"] == "CLOSED_TERMINAL":
                continue          # tracked in another stream; not ours to show
            b = _BUCKET.get(c["state"], "closed")
            buckets[b] += 1
            bucket_paise[b] += c["at_risk_paise"]

        won = sum(c["recovered_paise"] for c in mine)
        # Money that turned out to belong to another stream, or that resolved
        # outside the campaign entirely, is NOT at risk here. Counting it would
        # inflate this stream by exactly the amount another stream already
        # carries -- which is the double-count that made the receivables
        # balance appear twice.
        elsewhere = sum(c["at_risk_paise"] for c in mine
                        if c["state"] == "CLOSED_TERMINAL")
        closed_dry = sum(c["at_risk_paise"] for c in mine
                         if c["terminal"] and c["state"] not in
                         ("RECOVERED", "CLOSED_TERMINAL"))
        at_risk = sum(c["at_risk_paise"] for c in mine) - elsewhere
        r = WF.readiness(wf, items, caps, _SOURCES, _CAPS_FOR)
        shape = WF.ladder_shape(wf, cfg, wf_cfg)

        out.append({
            "key": wf.key, "title": wf.title, "plain": wf.plain,
            "detect": wf.detect, "diagnose": wf.diagnose,
            "diagnoses": wf.diagnoses, "unit": wf.unit,
            "mandate_backed": wf.mandate_backed,
            "promise_to_pay": wf.promise_to_pay,
            "n": sum(1 for c in mine if c["state"] != "CLOSED_TERMINAL"),
            "at_risk_paise": at_risk,
            "recovered_paise": won,
            "closed_dry_paise": closed_dry,
            "elsewhere_paise": elsewhere,
            "chasing_paise": max(at_risk - won - closed_dry, 0),
            "declined_n": len(declined),
            "declined_paise": sum(i.amount_paise for i in declined),
            "buckets": buckets, "bucket_paise": bucket_paise,
            "references": [c["reference"] for c in mine
                           if c["state"] != "CLOSED_TERMINAL"],
            "ladder": shape,
            "ready": r.ready, "blockers": r.blockers, "notes": r.notes,
            # TWO STATUSES, NOT ONE. `ready` answers "can this account feed
            # this loop"; `loop` answers "does the loop work". A console that
            # shows only the first renders two working loops as red BLOCKED
            # panels with nothing beside them, which is the wrong conclusion
            # from a true fact. One is fixed by a dashboard toggle or a busier
            # merchant; the other would be a bug.
            "loop": _loop_block(wf.key, items),
            # WHOSE MONEY IS THIS. The ledger stamps every capture with the
            # mode the gateway reported -- REAL for Razorpay, LOCAL for the
            # book this project holds. Carried up here because the console now
            # shows four streams side by side, and a Rs 86,673 figure sitting
            # next to a Rs 28,433 one with nothing to tell them apart is how a
            # careful project starts overstating itself. Rs 28,433 is still
            # the only money that has moved through a payment provider.
            "recovered_mode": _recovered_mode(wf.kind, mine),
        })

    # The pipeline as a graph, once every stream block exists.
    for block in out:
        wf = WF.load(wf_cfg)[block["key"]]
        block["graph"] = build_graph(
            wf, block, campaigns, cfg, wf_cfg, LEDGER,
            degradation=deg if wf.key == "payment_degradation" else None).as_dict()
        if wf.key == "payment_degradation":
            block["scan"] = deg
    return out


def _loop_block(key: str, items: list) -> dict:
    """Run the loop and report what came out. Cached per key within a build."""
    if key in _LOOP_CACHE:
        return _LOOP_CACHE[key]
    r = LOOPCHECK.check(key, items)
    _LOOP_CACHE[key] = {
        "status": r.status, "source": r.source, "headline": r.headline,
        "detail": [d for d in r.detail if d], "error": r.error,
    }
    return _LOOP_CACHE[key]


_LOOP_CACHE: dict[str, dict] = {}


def _recovered_mode(kind: str, mine: list) -> str:
    """REAL, LOCAL, or empty when nothing has been recovered on this stream."""
    refs = {c["reference"] for c in mine if c.get("recovered_paise")}
    if not refs:
        return ""
    modes = set()
    for path in (LEDGER, SUBS_LEDGER):
        if not path.exists():
            continue
        for e in L.read(path):
            if (e["event"] == L.ATTEMPT_SUCCEEDED
                    and e["reference"] in refs):
                modes.add(e.get("execution_mode") or "UNKNOWN")
    if modes == {"REAL"}:
        return "REAL"
    if "REAL" in modes:
        return "MIXED"
    return "LOCAL" if modes else ""


_SOURCES: dict = {}
_CAPS_FOR: dict = {}


def channels_block() -> list[dict]:
    """What each way of reaching someone costs and is bound by.

    Voice is the reason this is reported: TRAI restricts commercial calls to a
    narrower window than our own messaging rule, and a console that showed one
    global quiet-hours figure would be describing a rule the system does not
    actually apply to calls.
    """
    wf_cfg = CH.load_workflows()
    out = []
    for ch in CH.all_channels(wf_cfg):
        out.append({
            "key": ch.key, "plain": ch.plain, "contacting": ch.contacting,
            "cost_paise": ch.cost_paise,
            "hours_ist": list(ch.hours_ist) if ch.hours_ist else None,
            "respects_dnd": ch.respects_dnd,
            "min_at_risk_paise": ch.min_at_risk_paise,
            "requires_prior_contact": ch.requires_prior_contact,
            "language": ch.language, "executable": ch.executable,
            "terminal": ch.terminal,
            "unavailable_reason": ch.unavailable_reason,
        })
    return out


def ops_block(now: datetime, cfg: dict) -> dict:
    """Everything an operator does rather than watches.

    The review queue, the notification ledger, webhook ingestion, the
    suppression list and the gateway's own admission of what it cannot do.
    These were the gaps the prior-art review found; the console shows them
    because a control nobody can see is a control nobody trusts.
    """
    caps = get_gateway("razorpay").capabilities()
    q = review.queue(now, cfg, LEDGER, REVIEW)
    return {
        "review": {
            "summary": review.summary(now, cfg, LEDGER, REVIEW),
            "items": [{"reference": i.reference,
                       "at_risk_paise": i.at_risk_paise,
                       "decline_class": i.decline_class,
                       "escalated_at": i.escalated_at.isoformat(),
                       "age_band": i.ageing_band(now),
                       "state": i.state, "claimed_by": i.claimed_by,
                       "attempts_made": i.attempts_made,
                       "customer_ref": i.customer_ref} for i in q],
        },
        "notifications": notify.summary(NOTIF),
        "webhooks": WH.summary(WEBHOOKS),
        "suppressed": len(suppression.suppressed_set(SUPPRESS)),
        "capabilities": {"name": caps.name,
                         "signature": caps.webhook_signature,
                         "cannot": caps.explain(), "notes": caps.notes},
        "by_class": [{"key": b.key, "campaigns": b.campaigns,
                      "contacts": b.contacts, "recovered": b.recovered,
                      "recovered_paise": b.recovered_paise,
                      "at_risk_paise": b.at_risk_paise, "rate": b.rate,
                      "warning": b.sample_warning()}
                     for b in reports.by_decline_class(now, cfg, LEDGER)],
        "by_attempt": reports.by_attempt(now, LEDGER),
        "over_time": reports.over_time(now, 14, LEDGER),
    }


def ceiling_block(cfg: dict) -> dict:
    """The recovery-rate finding, computed rather than transcribed.

    Runs the same model as `scripts.recovery_curve`, so the console cannot
    quietly drift from the analysis it is reporting.
    """
    p = RC.fit_p(RC.NO_AUTOMATION[0], RC.BOOK_A)
    out = {"fitted_p": p, "no_automation": RC.NO_AUTOMATION,
           "comprehensive": RC.COMPREHENSIVE, "books": []}
    for name, mix, mandate in (
            ("One-off checkout, no mandate", RC.BOOK_A, False),
            ("Recurring subscription, mandate present", RC.BOOK_B, True)):
        att = RC.ladder(cfg, mandate)
        curve = RC.simulate(p, att, mix, np.random.default_rng(RC.SEED))
        out["books"].append({
            "name": name, "this_account": not mandate,
            "curve": [float(x) for x in curve],
            "final": float(curve[-1]),
            "ceiling": sum(mix[c] * RC.RECOVERABLE[c] for c in mix),
            "attempts": {c: att[c] for c in mix if mix[c] > 0},
        })
    no_mandate_b = RC.simulate(p, RC.ladder(cfg, False), RC.BOOK_B,
                               np.random.default_rng(RC.SEED))[-1]
    out["book_b_without_mandate"] = float(no_mandate_b)
    return out


def frozen_block() -> dict:
    """The offline validation, kept as evidence rather than headline."""
    p = FROZEN / "results_base.json"
    if not p.exists():
        return {}
    base = json.loads(p.read_text(encoding="utf-8"))
    tp = sum(r["tp"] for r in base)
    fn = sum(r["fn"] for r in base)
    fp = sum(r["fp"] for r in base)
    ok = sum(r["arms"]["B2"]["attr_correct"] for r in base)
    ap = sum(r["arms"]["B2"]["attr_applicable"] for r in base)
    nulls = [r for r in base if r["kind"] == "none"]
    rl, rh = wilson_interval(tp, tp + fn)

    l3p = Path("data/dev/l3_eval.json")
    l3 = json.loads(l3p.read_text(encoding="utf-8")) if l3p.exists() else []
    got = [r for r in l3 if r.get("root")]

    dev = Path("data/dev/baselines_base.json")
    after = None
    if dev.exists():
        rows = json.loads(dev.read_text(encoding="utf-8"))
        dn = [r for r in rows if r["kind"] == "none"]
        if dn:
            after = sum(1 for r in dn
                        if r["arms"]["B2"]["interventions"] > 0) / len(dn)

    return {
        "n_scenarios": len(base),
        "recall": tp / max(tp + fn, 1), "recall_ci": [rl, rh],
        "precision": tp / max(tp + fp, 1),
        "attribution_accuracy": ok / max(ap, 1),
        "false_intervention_before": sum(
            1 for r in nulls if r["arms"]["B2"]["interventions"] > 0
        ) / max(len(nulls), 1),
        "false_intervention_after": after,
        "l3_accuracy": (sum(1 for r in got if r["correct"]) / len(got)) if got else 0,
        "l3_chance": (float(np.mean([r["chance_within_affected"] for r in got]))
                      if got else 0),
    }


def main() -> None:
    cfg = load_declines()
    now = datetime.now(IST)
    items = collect()

    live = campaigns_block(now, cfg)
    payload = {
        "meta": {
            "generated_at": now.isoformat(),
            "policy_version": cfg["policy_version"],
            "account": account_summary(),
            "quiet_hours": cfg["compliance"]["quiet_hours_ist"],
            "max_contacts": cfg["compliance"]["max_contacts_per_reference"],
            "exposure_floor_paise": cfg["stopping"]["min_at_risk_paise"],
            # The full state vocabulary, in order, so the console can render
            # every state the machine has -- including the ones currently at
            # zero. A judge should see what CAN happen, not only what has.
            "state_vocabulary": V._ORDER,
            "terminal_states": sorted(V.TERMINAL),
        },
        "live": live,
        "at_risk_all_paise": sum(i.amount_paise for i in items),
        "n_items": len(items),
        "n_declined": len(items) - len(live["campaigns"]),
        "projection": projection_block(now, cfg),
        "streams": streams_block(live["campaigns"], items, now, cfg),
        "channels": channels_block(),
        "promises": promises.summary(now, CH.load_workflows(), PROMISES),
        "promise_rows": promises.read(PROMISES)[-10:],
        "ops": ops_block(now, cfg),
        "ceiling": ceiling_block(cfg),
        "frozen": frozen_block(),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("window.RECOVERY_DATA = "
                   + json.dumps(payload, indent=1, default=str) + ";\n",
                   encoding="utf-8")
    print(f"campaigns   {len(live['campaigns'])} "
          f"({live['n_open']} open, {live['n_terminal']} closed)")
    print(f"states      " + ", ".join(f"{s}:{n}" for s, n in live["states"]))
    print(f"projection  day {payload['projection']['horizon_days']}: "
          + ", ".join(f"{s}:{n}" for s, n in payload["projection"]["states"]))
    print(f"recovered   Rs {live['recovered_paise']/100:,.0f}")
    ops = payload["ops"]
    print(f"review      {ops['review']['summary']['open']} open, "
          f"{ops['suppressed']} suppressed contact(s)")
    for st in payload["streams"]:
        flag = "" if st["ready"] else "  [blocked]"
        print(f"  {st['title']:<22} {st['n']:>3} cases  "
              f"Rs {st['at_risk_paise']/100:>10,.0f} at risk  "
              f"Rs {st['recovered_paise']/100:>9,.0f} back{flag}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
