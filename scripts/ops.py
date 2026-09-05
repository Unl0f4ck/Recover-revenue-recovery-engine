"""Operator commands for the recovery engine.

    python -m scripts.ops report                    # daily reconciliation
    python -m scripts.ops report --drift            # ...and check the gateway
    python -m scripts.ops analytics                 # what the ladder recovers
    python -m scripts.ops queue                     # manual-review queue
    python -m scripts.ops claim  <ref> --who jo
    python -m scripts.ops resolve <ref> --who jo --note "card replaced"
    python -m scripts.ops opt-out <email|phone> --reason "replied STOP"
    python -m scripts.ops suppressed                # who is on the list
    python -m scripts.ops link <email> --purpose opt_out
    python -m scripts.ops import <file.csv> --unit rupees
    python -m scripts.ops webhook <file.json>       # replay a saved event
    python -m scripts.ops capabilities              # what the gateway cannot do
    python -m scripts.ops workflows                 # the four loops and their state
    python -m scripts.ops channels                  # how we reach people, and the rules
    python -m scripts.ops promise <ref> --by 2026-09-05 --note "called, will pay Fri"
    python -m scripts.ops promises                  # who has promised what
    python -m scripts.ops floors                    # why each threshold is what it is
    python -m scripts.ops reply <ref> --text "..." --customer a@b.com  # read a reply
    python -m scripts.ops ai                        # which model provider is configured
    python -m scripts.ops merchants

`--merchant <id>` selects a merchant from config/merchants.yaml; everything runs
as `default` (the live Razorpay test account) otherwise.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from src.gateways import get_gateway
from src.ingest.csv_source import read_csv
from src.recovery import (channels, merchants, notify, promises, reports,
                          review, suppression, tokens)
from src.recovery import loopcheck as LOOPCHECK
from src.recovery import workflows as WF
from src.recovery import webhooks as WH
from src.recovery import runlock
from src.recovery.campaign import IST


def arg(name: str, default=None, cast=str):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return default


def rs(paise) -> str:
    return f"Rs {(paise or 0)/100:,.0f}"


def positional(n: int = 2) -> str | None:
    rest = [a for a in sys.argv[1:] if not a.startswith("--")]
    return rest[n - 1] if len(rest) >= n else None


def main() -> None:
    cmd = positional(1) or "report"
    m = merchants.get(arg("--merchant", merchants.DEFAULT))
    now = datetime.now(IST)
    p = m.paths()

    if m.id != merchants.DEFAULT:
        print(f"merchant: {m.id} ({m.name})\n")

    # ---------------------------------------------------------------- report
    if cmd == "report":
        rep = reports.daily(now, cfg=m.cfg, path=p["path"],
                            notif_path=p["notif_path"], review_path=m.review,
                            check_drift="--drift" in sys.argv)
        print(f"DAILY RECONCILIATION  {rep.since:%d %b %H:%M} -> {now:%d %b %H:%M}")
        print(f"  opened            {rep.opened}")
        print(f"  contacts made     {rep.contacts}")
        print(f"  deferred          {rep.deferred}  (quiet hours / caps)")
        print(f"  suppressed        {rep.suppressed}  (declined to send)")
        print(f"  RECOVERED         {rep.recovered_count}  {rs(rep.recovered_paise)}")
        print(f"  closed            {rep.closed}")
        print()
        print(f"  still open        {rep.still_open}  "
              f"{rs(rep.at_risk_open_paise)} at risk")
        print(f"  awaiting a human  {rep.review.get('open', 0)} open, "
              f"{rep.review.get('claimed', 0)} claimed, oldest "
              f"{rep.review.get('oldest_days', 0)}d")
        if rep.stop_reasons:
            print("\n  CLOSED, AND WHY")
            for r, n in rep.stop_reasons:
                print(f"    {n:>4}  {r}")
        if rep.notification_states:
            print("\n  NOTIFICATIONS")
            for k, n in sorted(rep.notification_states.items(),
                               key=lambda kv: -kv[1]):
                print(f"    {n:>4}  {k}")
        if "--drift" in sys.argv:
            print(f"\n  DRIFT vs the gateway  ({len(rep.drift)} disagreement(s))")
            for d in rep.drift:
                print(f"    {d.reference:<24} ledger says {d.ledger_says}, "
                      f"gateway says {d.gateway_says}  {rs(d.amount_paise)}")
            if not rep.drift:
                print("    none -- the ledger and the gateway agree")
        return

    # ------------------------------------------------------------- analytics
    if cmd == "analytics":
        print("RECOVERY BY DECLINE CLASS")
        print(f"  {'class':<20}{'campaigns':>10}{'contacts':>10}"
              f"{'recovered':>11}{'rate':>8}   note")
        for b in reports.by_decline_class(now, m.cfg, p["path"]):
            print(f"  {b.key:<20}{b.campaigns:>10}{b.contacts:>10}"
                  f"{b.recovered:>11}{b.rate:>8.0%}   {b.sample_warning()}")
        print("\nRECOVERY BY ATTEMPT  -- which rung of the ladder pays for itself")
        rows = reports.by_attempt(now, p["path"])
        if not rows:
            print("  nothing attempted yet")
        for r in rows:
            note = "n too small to read as a rate" if r["thin"] else ""
            print(f"  attempt {r['attempt']}   {r['contacts']:>4} contacts   "
                  f"{r['recovered']:>3} recovered   {r['rate']:>6.0%}   {note}")
        print("\nOVER TIME")
        for r in reports.over_time(now, 14, p["path"]):
            print(f"  {r['day']}   opened {r['opened']:>3}   "
                  f"contacts {r['contacts']:>3}   recovered {r['recovered']:>3}"
                  f"   {rs(r['recovered_paise'])}")
        return

    # ----------------------------------------------------------------- queue
    if cmd == "queue":
        q = review.queue(now, m.cfg, p["path"], m.review,
                         include_closed="--all" in sys.argv)
        s = review.summary(now, m.cfg, p["path"], m.review)
        print(f"MANUAL REVIEW  {s['open']} open, {s['claimed']} claimed, "
              f"{rs(s['at_risk_paise'])} at risk")
        if not q:
            print("  nothing awaiting a human")
            return
        print(f"\n  {'reference':<26}{'amount':>12}  {'class':<18}"
              f"{'age':<12}{'state':<10}who")
        for i in q:
            print(f"  {i.reference[:26]:<26}{rs(i.at_risk_paise):>12}  "
                  f"{i.decline_class:<18}{i.ageing_band(now):<12}"
                  f"{i.state:<10}{i.claimed_by or '-'}")
        return

    if cmd in ("claim", "resolve", "dismiss", "reopen"):
        ref = positional(2)
        who = arg("--who")
        note = arg("--note", "")
        if not ref or not who:
            raise SystemExit(f"usage: ops {cmd} <reference> --who <name>"
                             + ("" if cmd == "claim" else " --note <why>"))
        fn = {"claim": review.claim, "resolve": review.resolve,
              "dismiss": review.dismiss, "reopen": review.reopen}[cmd]
        if cmd == "claim":
            fn(ref, who, now, note, path=m.review)
        else:
            fn(ref, who, now, note, path=m.review)
        print(f"{cmd}ed {ref} ({who})" + (f": {note}" if note else ""))
        return

    # --------------------------------------------------------------- opt-out
    if cmd == "opt-out":
        who = positional(2)
        if not who:
            raise SystemExit("usage: ops opt-out <email|phone> [--reason ...]")
        suppression.suppress(who, now, reason=arg("--reason", "operator request"),
                             source="cli", path=p["sup_path"])
        print(f"suppressed {who}. No campaign will contact them again.")
        return

    if cmd == "suppressed":
        s = suppression.suppressed_set(p["sup_path"])
        print(f"SUPPRESSED CONTACTS  ({len(s)})")
        for who in sorted(s):
            print(f"  {who:<38}{suppression.reason_for(who, p['sup_path'])}")
        return

    if cmd == "link":
        who = positional(2)
        purpose = arg("--purpose", tokens.OPT_OUT)
        from src.execution.razorpay import load_env
        token_env = load_env()
        secret = arg("--secret") or token_env.get("RECOVERY_TOKEN_SECRET")
        if not secret:
            raise SystemExit("Set RECOVERY_TOKEN_SECRET to sign customer links")
        base = token_env.get("RECOVERY_PUBLIC_URL") or m.base_url or "http://localhost:8000"
        if not who:
            raise SystemExit("usage: ops link <email|phone> [--purpose opt_out]")
        print(tokens.link(base, purpose, who, secret, now, reference=arg("--reference")))
        if secret.startswith("dev-"):
            print("\n  NOTE: signed with a development secret. A real deployment "
                  "must set its own\n  and keep it server-side; the token carries "
                  "the customer reference, so an\n  unsigned or weakly-signed one "
                  "lets anyone opt out anyone else.")
        return

    # ---------------------------------------------------------------- import
    if cmd == "import":
        src = positional(2)
        if not src:
            raise SystemExit("usage: ops import <file.csv> [--unit rupees]")
        res = read_csv(src, arg("--unit", "paise"), now)
        print(f"CSV IMPORT  {src}")
        print(f"  read      {len(res.items)} item(s), {rs(res.at_risk_paise)} at risk")
        print(f"  rejected  {len(res.rejected)}")
        for r in res.rejected[:10]:
            print(f"    line {r['line']}: {r['error']}")
        if res.rejected:
            print("  Rejected rows are REPORTED, never skipped -- a silent drop "
                  "is how a\n  merchant concludes recovery does not work for them.")
        from src.recovery.campaign import classify_item
        by: dict[str, int] = {}
        for it in res.items:
            k = classify_item(it, m.cfg).decline_class
            by[k] = by.get(k, 0) + 1
        print("\n  WOULD CLASSIFY AS")
        for k, n in sorted(by.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>4}  {k}")
        print(f"\n  Preview only. Run: python -m scripts.run_dunning --csv \"{src}\" --unit {res.amount_unit}")
        return

    # --------------------------------------------------------------- webhook
    if cmd == "webhook":
        src = positional(2)
        secret = arg("--secret") or "whsec_dev"
        if not src:
            raise SystemExit("usage: ops webhook <file.json> [--secret ...]")
        raw = Path(src).read_bytes()
        import hashlib
        import hmac
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        with runlock.exclusive(label="operator webhook replay"):
            got = WH.handle(raw, {"X-Razorpay-Signature": sig,
                              "x-razorpay-event-id": arg("--event-id", "evt_local")},
                        secret, now, cfg=m.cfg, path=p["path"],
                        wh_path=m.webhooks)
        print(f"WEBHOOK  {got.kind}  ->  {got.applied}"
              + ("  (duplicate)" if got.duplicate else ""))
        print(f"  {got.detail or 'nothing to do'}")
        print(f"\n  {json.dumps(WH.summary(m.webhooks))}")
        return

    # ---------------------------------------------------------- capabilities
    if cmd == "capabilities":
        caps = get_gateway(m.gateway).capabilities()
        print(f"GATEWAY  {caps.name}")
        print(f"  webhook signature   {caps.webhook_signature}")
        print(f"  artefact ceiling    {caps.max_recovery_artefacts}")
        print("\n  WHAT IT CANNOT DO  -- every honesty caveat in this project "
              "is one of these")
        for line in caps.explain():
            print(f"    - {line}")
        if caps.notes:
            print(f"\n  {caps.notes}")
        return

    # -------------------------------------------------------------- workflows
    if cmd == "workflows":
        from src.recovery.runner import collect
        wf_cfg = channels.load_workflows()
        items = collect()
        caps = get_gateway(m.gateway).capabilities()

        # WHICH BOOK IS ANSWERING for each loop. Two of the four cannot be
        # served by this Razorpay account -- one is starved, one is gated -- so
        # they read books this project holds. The live account is still tried
        # first for both, every time.
        from src.ingest import subscription_source as _SS
        from src.ingest import traffic_source as _TS
        sources = {"payment_degradation": _TS.collect().source}
        try:
            _SS.fetch_subscriptions(1)
            sources["subscription_failure"] = "live"
        except _SS.SubscriptionsUnavailable:
            sources["subscription_failure"] = (
                "local" if _SS.read_local() else "none")
        subs = _SS.read_local() if sources["subscription_failure"] == "local" else []
        items = list(items) + subs
        caps_for = {}
        if sources["subscription_failure"] == "local":
            caps_for["subscription_failure"] = get_gateway("local").capabilities()

        print("THE FOUR RECOVERY LOOPS")
        for wf in WF.ordered(wf_cfg):
            sh = WF.ladder_shape(wf, m.cfg, wf_cfg)
            r = WF.readiness(wf, items, caps, sources, caps_for)
            mine = [i for i in items if i.kind == wf.kind]
            print()
            # TWO STATUSES, because they answer different questions and one
            # label conflating them reads as "broken" when it means "your
            # account cannot feed this". A merchant with volume, or a
            # dashboard toggle, fixes the first. Only a bug fixes the second.
            src = sources.get(wf.key)
            live = ("running on live data" if r.ready and src != "local"
                    else "running on a local book" if src == "local"
                    else "NOT ON THIS ACCOUNT")
            print(f"  {wf.title.upper()}   {live}")
            print(f"    {wf.plain}")
            print(f"    found by      {wf.detect[:96]}")
            print(f"    explained by  "
                  f"{wf.diagnose[:96] if wf.diagnoses else 'nothing to explain'}")
            print(f"    at risk       {len(mine)} {wf.unit}(s), "
                  f"{rs(sum(i.amount_paise for i in mine))}")
            print(f"    plan          {sh['steps']} steps over {sh['span_days']}d, "
                  f"{sh['contacts']} reach the customer"
                  + (f", {sh['silent']} free" if sh['silent'] else "")
                  + f", ends with {sh['ends_with']}")
            for n in r.notes:
                print(f"    note          {n}")
            for b in r.blockers:
                print(f"    why not       {b}")
            if not r.ready or "--check" in sys.argv:
                lc = LOOPCHECK.check(wf.key, items)
                mark = {"works": "LOOP WORKS", "BROKEN": "LOOP BROKEN",
                        "not run": "not checked"}[lc.status]
                print(f"    {mark:<13} {lc.headline}")
                for d in lc.detail:
                    if d:
                        print(f"      {d}")
                if lc.error:
                    print(f"      ERROR  {lc.error}")
        print()
        print("  'NOT ON THIS ACCOUNT' is a fact about the account, not the")
        print("  code: one loop is starved of volume, the other is gated at")
        print("  401. Both are run above against a stream to show they work.")
        return

    if cmd == "channels":
        wf_cfg = channels.load_workflows()
        print("WAYS OF REACHING SOMEONE")
        print(f"  {'channel':<40}{'hours (IST)':<14}{'cost':>10}   status")
        for ch in channels.all_channels(wf_cfg):
            if not ch.contacting:
                continue
            hrs = (f"{ch.hours_ist[0]:02d}:00-{ch.hours_ist[1]:02d}:00"
                   if ch.hours_ist else "any")
            cost = (f"{ch.cost_paise} paise" if ch.cost_paise < 100
                    else f"Rs {ch.cost_paise/100:.2f}")
            status = "in use" if ch.executable else "NOT CONNECTED"
            print(f"  {ch.plain[:38]:<40}{hrs:<14}{cost:>10}   {status}")
            if ch.respects_dnd:
                print(f"    ^ TRAI window and DND registry; narrower than our "
                      f"own messaging rule at both ends")
            if ch.min_at_risk_paise:
                print(f"    ^ reserved for exposure over "
                      f"{rs(ch.min_at_risk_paise)}, and never before a softer "
                      f"approach")
            if not ch.executable and ch.unavailable_reason:
                print(f"    ^ {ch.unavailable_reason}")
        return

    # ------------------------------------------------------ promise to pay
    if cmd == "promise":
        ref, by = positional(2), arg("--by")
        if not ref or not by:
            raise SystemExit("usage: ops promise <reference> --by YYYY-MM-DD "
                             "[--note ...] [--amount <rupees>]")
        pay_by = datetime.fromisoformat(by).replace(tzinfo=IST)
        try:
            pr = promises.record(
                ref, pay_by, now,
                amount_paise=int(float(arg("--amount", 0)) * 100),
                channel=arg("--via", "phone"), note=arg("--note", ""),
                recorded_by=arg("--who", "cli"),
                cfg=channels.load_workflows(), path=m.promise)
            print(f"recorded: {ref} will pay by {pr.pay_by:%d %b}")
            print("  the ladder is PAUSED until then. Chasing someone who")
            print("  has already named a date is how you lose a customer")
            print("  who was going to pay.")
            if pr.broken_count:
                print(f"  NOTE: this debtor has broken {pr.broken_count} "
                      f"promise(s) before.")
        except promises.PromiseRefused as e:
            raise SystemExit(f"refused: {e}")
        return

    if cmd == "promises":
        s_ = promises.summary(now, channels.load_workflows(), m.promise)
        print(f"PROMISES TO PAY  {s_['active']} active, "
              f"{rs(s_['amount_paise'])}, {s_['kept']} kept, "
              f"{s_['broken']} broken")
        rows = promises.read(m.promise)
        for r in rows[-15:]:
            when = (r.get("pay_by") or "")[:10]
            print(f"  {r['at'][:10]}  {r['reference'][:26]:<26}"
                  f"{r['action']:<10}{when:<12}{r.get('note','')[:40]}")
        if not rows:
            print("  none recorded")
        return

    # ---------------------------------------------------------------- floors
    if cmd == "floors":
        from src.recovery import economics as EC
        wf_cfg = channels.load_workflows()
        print("WHY EACH THRESHOLD IS WHAT IT IS")
        print()
        print("  Two different questions, and only the first has an arithmetic")
        print("  answer:")
        print("    cash floor    below this, chasing genuinely LOSES money")
        print("    policy floor  below this, it is not worth INTERRUPTING them")
        print()
        print(f"  {'workflow':<22}{'cash':>7}{'contacts':>9}{'each':>8}"
              f"{'goodwill':>10}{'cash floor':>12}{'THRESHOLD':>11}")
        for w in WF.ordered(wf_cfg):
            e = EC.for_workflow(w, m.cfg, wf_cfg)
            print(f"  {w.title:<22}{e.total_cost_paise:>6}p{e.contacts:>9}"
                  f"{e.nuisance_per_contact_paise/100:>7.0f}"
                  f"{e.nuisance_cost_paise/100:>10.0f}"
                  f"{rs(e.cash_floor_paise):>12}{rs(e.policy_floor_paise):>11}")
        print()
        print("  Messaging is nearly free, so the cash floor is tens of rupees.")
        print("  What actually sets the threshold is the price of interrupting")
        print("  someone -- goodwill and sender reputation, neither of which")
        print("  scales with the amount at stake.")
        print()
        print("  HOW SENSITIVE  -- the threshold at other prices per contact")
        print(f"  {'price each':<14}" +
              "".join(f"{w.title[:16]:>18}" for w in WF.ordered(wf_cfg)))
        for price in (0, 1000, 2500, 6000, 15000, 30000):
            row = f"  {rs(price):<14}"
            for w in WF.ordered(wf_cfg):
                base = EC.for_workflow(w, m.cfg, wf_cfg)
                alt = EC.economics_for(
                    w.key, m.cfg["schedules"][w.schedule]["steps"],
                    base.recovery_probability, price, "",
                    set(m.cfg["compliance"]["contacting_channels"]), wf_cfg)
                mark = " *" if price == base.nuisance_per_contact_paise else ""
                row += f"{rs(alt.policy_floor_paise) + mark:>18}"
            print(row)
        print()
        print("  * the price currently set. At zero the threshold collapses to")
        print("    the cash floor, which is the whole point: everything above it")
        print("    is the value we place on not bothering people.")
        print()
        for w in WF.ordered(wf_cfg):
            e = EC.for_workflow(w, m.cfg, wf_cfg)
            print(f"  {w.title.upper()}  -> {rs(e.policy_floor_paise)}")
            print(f"    {e.policy_reason}")
            print()
        return

    # -------------------------------------------------------------------- ai
    if cmd == "ai":
        from src.ai import provider as AP
        got = AP.available()
        print("MODEL PROVIDER")
        print(f"  configured    {', '.join(got) if got else 'NONE'}")
        if got:
            pr = AP.get_provider()
            print(f"  in use        {pr.name} / {pr.model}")
        else:
            print("  Add GEMINI_API_KEY to .env -- free key at")
            print("  https://aistudio.google.com/apikey")
        print()
        print("  WHAT IT IS ALLOWED TO DO")
        print("    read a customer reply and say what it thinks they meant")
        print("  WHAT IT IS NOT ALLOWED TO DO")
        print("    decide anything. Every date it proposes goes through the")
        print("    same validation an operator's typing does; a hallucinated")
        print("    date is refused by exactly the code that refuses a typo.")
        return

    if cmd == "reply":
        from src.ai import provider as AP
        from src.ai import replies as RP
        ref, text = positional(2), arg("--text")
        if not ref or not text:
            raise SystemExit('usage: ops reply <reference> --text "..."')
        try:
            r = RP.read_reply(text, now, reference=ref)
        except AP.LLMUnavailable as e:
            # No key at all is a setup problem, and telling the operator to go
            # and fix it is more useful than filing the reply for review.
            raise SystemExit(f"{e}")
        except AP.LLMError as e:
            # Reachable but unusable -- out of quota, at capacity, malformed
            # answer. The reply still exists and still needs handling, so it
            # goes to a person rather than being lost to a traceback.
            print(f"  !! could not read this one: {str(e).splitlines()[0][:90]}")
            r = RP.unreadable(text, "the model could not be reached")
        print(f"REPLY FROM {ref}")
        print(f'  "{text}"')
        print()
        print(f"  read as       {r.intent}  ({r.confidence:.0%} sure)")
        print(f"  by            {r.provider}/{r.model}")
        if r.pay_by:
            print(f"  date          {r.pay_by:%d %b %Y}")
        print(f"  because       {r.reasoning}")
        print()
        if "--apply" not in sys.argv:
            print("  nothing done. Pass --apply to act on it.")
            print(f"  would        {'act automatically' if r.actionable else r.why()}")
            return
        got = RP.apply_reading(r, ref, now, channels.load_workflows(),
                               promise_path=m.promise, sup_path=p["sup_path"],
                               review_path=m.review,
                               customer_ref=arg("--customer"))
        print(f"  ACTION        {got.action}  --  {got.detail}")
        return

    if cmd == "merchants":
        for mid in merchants.known():
            mm = merchants.get(mid)
            print(f"  {mid:<14}{mm.name:<38}{mm.gateway:<12}"
                  f"attempts={mm.cfg['stopping']['max_attempts']:<4}"
                  f"contacts={mm.cfg['compliance']['max_contacts_per_reference']}")
        return

    if cmd == "degradation":
        # The one loop that cannot be shown on this account, because the
        # detector is starved rather than broken. `--live` runs it against real
        # traffic anyway and reports the thin-book verdict, which is the honest
        # state; without it, simulated traffic gives the loop enough volume to
        # actually reach a decision.
        from src.ingest import traffic_source as TS
        from src.recovery.degradation import scan
        t = TS.collect(allow_local="--live" not in sys.argv)
        pays, truth = t.payments, None
        print("PAYMENT DEGRADATION")
        print(f"  source          {t.source} -- {t.note}")
        if t.source == "local":
            print("  The detector, the attribution ladder, the policy and the")
            print("  frozen thresholds are production code, and none of them")
            print("  is told where the incident is or that there is one.")
        print()
        res = scan(pays)
        print(f"  payments seen   {res.payments_seen}")
        print(f"  baseline rate   {res.baseline_rate:.1%}")
        print(f"  cells           {len(res.cells)}, largest {res.largest_cell}")
        if res.thin:
            print(f"  THIN            needs {res.min_volume} attempts in one "
                  f"segment; largest here is {res.largest_cell}")
        print()
        if truth:
            print(f"  INJECTED        {truth.describe()}")
            print("                  (ground truth, never shown to the scan)")
            print()
        if not res.incident:
            print(f"  CONCLUSION      {res.conclusion}")
            return
        for c in res.alerting_cells:
            print(f"  ALERTING        {c.segment}/{c.method}  "
                  f"{c.failures}/{c.attempts} = {c.rate:.0%}   "
                  f"{rs(c.at_risk_paise)} at risk")
        d = res.diagnosis
        print()
        print(f"  cause           {d.cause_node}")
        print(f"  mechanism       {d.mechanism_family}")
        print(f"  confidence      {d.confidence:.2f}   (level {d.level_used})")
        print(f"  signature       {d.evidence['dominant_source']} / "
              f"{d.evidence['dominant_step']}")
        print()
        print(f"  ACTION          {res.action}")
        print("  An incident is fixed by moving traffic, not by messaging the")
        print("  customers who happened to be caught in it.")
        return

    if cmd == "subscriptions":
        from src.ingest import subscription_source as SS
        print("SUBSCRIPTION FAILURE")
        try:
            got = SS.collect()
        except SS.SubscriptionsUnavailable as e:
            print("  BLOCKED ON THIS ACCOUNT")
            print(f"    {e}")
            print()
            print("  The loop is built in full and the ingest above reads the")
            print("  real API. Nothing else changes when it is enabled.")
            print()
            from src.recovery.declines import load_declines as _ld
            cfg = _ld()
            steps = cfg["schedules"]["mandate"]["steps"]
            contacting = set(cfg["compliance"]["contacting_channels"])
            print("  THE MANDATE LADDER  (7 steps over 21 days)")
            for i, st in enumerate(steps):
                kind = ("silent, contacts nobody"
                        if st.get("requires_mandate")
                        else "reaches the customer"
                        if st["channel"] in contacting else "handover")
                print(f"    {i+1}. +{st['after_hours']:>4}h  "
                      f"{st['channel']:<24}{kind}")
            print()
            print("  Four of the seven rungs cost nothing and interrupt")
            print("  nobody. That is what a mandate buys, and it is the")
            print("  binding constraint on recovery rate in")
            print("  docs/RECOVERY_RATE.md -- 44.4% without, 68.4% with.")
            return
        print(f"  {len(got)} subscription(s) at risk, "
              f"{rs(sum(g.amount_paise for g in got))} per cycle")
        for g in got[:20]:
            d = g.detail or {}
            print(f"    {g.reference:<24}{rs(g.amount_paise):>10}  "
                  f"{d.get('status'):<9}{d.get('remaining_count')} cycles left")
        return

    raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
