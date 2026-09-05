"""Run the retry sequencer over the live account.

    python -m scripts.run_dunning                     # dry run, scratch ledger
    python -m scripts.run_dunning --horizon 14        # play the campaign forward
    python -m scripts.run_dunning --execute           # real links, real ledger
    python -m scripts.run_dunning --reconcile         # read outcomes back
    python -m scripts.run_dunning --opt-out a@b.com   # never contact them again

A dry run writes to a SCRATCH ledger, never the real one, so it can be run
freely and repeatedly without opening campaigns against real customers.

`--horizon N` steps a simulated clock forward through N days, running one pass
per hour and reporting what fires when. Nothing about the data is invented --
these are the real items at risk on the account, put through the real state
machine at the real cadence. It answers "what does this campaign actually do
over two weeks" without waiting two weeks, and it is the only way to see the
whole ladder before committing to it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from src.gateways import get_gateway
from src.recovery import channels as CH
from src.recovery import ledger as L
from src.recovery import merchants, runlock, suppression
from src.recovery.campaign import contact_for, delivery_for
from src.recovery.campaign import IST, reconcile_links, run_pass
from src.recovery.declines import load_declines
from src.recovery.runner import collect

REAL = Path("data/live/dunning_ledger.jsonl")
SUPPRESSION = Path("data/live/suppression.jsonl")
NOTIFICATIONS = Path("data/live/notifications.jsonl")
PROMISES = Path("data/live/promises.jsonl")
SCRATCH = Path("data/live/dunning_ledger.dryrun.jsonl")


def rs(paise) -> str:
    return f"Rs {paise/100:,.0f}"


def arg(name: str, default=None, cast=str):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return default


def main() -> None:
    # Operator command: remove someone from every campaign, now and future.
    if "--opt-out" in sys.argv:
        who = arg("--opt-out")
        suppression.suppress(who, datetime.now(IST),
                             reason=arg("--reason", "operator request"),
                             source="cli", path=SUPPRESSION)
        print(f"suppressed {who}. No campaign will contact them again.")
        print(f"  {len(suppression.suppressed_set(SUPPRESSION))} contact(s) "
              f"currently suppressed")
        return

    # WHICH BOOK. The default merchant is the live Razorpay test account.
    # `acme_subs` is a subscription book this project holds itself, served by
    # the `local` gateway, because Razorpay Subscriptions is not enabled on the
    # account and cannot be enabled from here. Separate ledgers, separate
    # config, separate gateway -- see config/merchants.yaml.
    m = merchants.get(arg("--merchant", merchants.DEFAULT))
    execute = "--execute" in sys.argv
    do_reconcile = "--reconcile" in sys.argv
    # DELIVERY IS OPT-IN PER RUN. `config/workflows.yaml` can enable it
    # permanently, but the flag is what a person types when they mean it, and
    # this is the only thing the sequencer does that reaches a stranger.
    wf_cfg = CH.load_workflows()
    dcfg = CH.delivery(wf_cfg)
    notify_on = "--notify" in sys.argv or bool(dcfg.get("enabled"))
    deliver = ({"sms": bool(dcfg.get("sms")), "email": bool(dcfg.get("email"))}
               if notify_on else None)
    horizon = arg("--horizon", 0, int)
    if horizon and execute:
        raise SystemExit("--horizon is a preview only; it cannot be combined with --execute")
    fresh = "--fresh" in sys.argv
    path = m.ledger if execute else (
        SCRATCH if m.id == merchants.DEFAULT
        else m.ledger.with_suffix(".dryrun.jsonl"))
    cfg = m.cfg
    SUPPRESSION_P, NOTIFICATIONS_P, PROMISES_P = (
        m.suppression, m.notifications, m.promise)
    if not execute:
        NOTIFICATIONS_P = path.with_name(path.stem + ".notifications.jsonl")
    gateway = None
    if m.gateway != "razorpay":
        gateway = get_gateway(m.gateway, base_url=m.base_url or
                              "https://recover.example.com")

    if not execute and (fresh or horizon):
        path.unlink(missing_ok=True)          # scratch only; the real ledger is append-only
        NOTIFICATIONS_P.unlink(missing_ok=True)

    print(f"RETRY SEQUENCER  --  {m.name}")
    if m.gateway != "razorpay":
        print(f"  gateway     {m.gateway} -- a book this project holds "
              f"itself; nothing here is REAL money")
    print(f"  mode        {'EXECUTE (real links, real ledger)' if execute else 'dry run (scratch ledger)'}")
    print(f"  ledger      {path}")
    print(f"  policy      {cfg['policy_version']}")
    if deliver:
        print(f"  delivery    ON -- Razorpay will be asked to send "
              f"({', '.join(k for k, v in deliver.items() if v)})")
        if not execute:
            print("              dry run, so nothing is sent; the recipients "
                  "are listed below")
    else:
        print("  delivery    off -- links are created, nobody is messaged "
              "(--notify to send)")
    print()

    if do_reconcile:
        with runlock.exclusive(label="reconcile"):
            n = reconcile_links(path=m.ledger, gateway=gateway, cfg=cfg)
        print(f"RECONCILED  {n} newly-paid link(s) recorded as recovered")
        print(f"  recovered so far   {rs(L.recovered_paise(m.ledger))}")
        print()

    csv_file = arg("--csv")
    if csv_file:
        from src.ingest.csv_source import read_csv
        imported = read_csv(Path(csv_file), arg("--unit", "paise"))
        items = imported.items
        print(f"CSV: {len(items)} accepted, {len(imported.rejected)} rejected")
        for row in imported.rejected:
            print(f"  rejected line {row['line']}: {row['error']}")
    elif m.source == "subscriptions":
        from src.ingest import subscription_source as SUBS
        items = SUBS.read_local() if m.gateway == "local" else SUBS.collect(allow_local=False)
    else:
        items = collect()
    # `--as-of N` evaluates the pass as though N minutes have passed. Nothing
    # about the data moves: these orders are real and genuinely unpaid, and the
    # schedule is the real one. Only the clock the due-date is compared against
    # advances, so a step that the ladder says fires in an hour can be shown
    # firing now. Every report states the offset it used.
    offset = arg("--as-of", 0, int)
    if offset and execute:
        # --as-of exists to PREVIEW a schedule, not to timestamp reality. Used
        # with --execute it writes future-dated events into the real ledger,
        # which then renders a recovery as having happened before the link that
        # produced it was sent. An audit trail that disagrees with causality is
        # worse than no audit trail.
        raise SystemExit(
            "refusing --as-of with --execute: it would write future-dated "
            "events into the real ledger. Use --as-of for dry runs, or "
            "--horizon to play a campaign forward.")
    now = datetime.now(IST) + timedelta(minutes=offset)
    if offset:
        print(f"  clock       evaluated as of now + {offset} min (dry run only)")
        print()
    print(f"REVENUE AT RISK   {rs(sum(i.amount_paise for i in items))} "
          f"across {len(items)} items")

    # First pass: open campaigns and fire anything already due.
    # Sequences are opened for EVERYTHING at risk -- opening costs nothing and
    # contacts nobody. `--max` caps only how many are ADVANCED this pass, which
    # is what `batch_limits.max_interventions_per_run` is for: it stops one run
    # touching the whole book. Test-mode Payment Links are also capped per
    # business, so an uncapped execute would exhaust them.
    try:
        with runlock.exclusive(label="run_dunning"):
            if execute and m.gateway == "razorpay":
                from src.recovery.service import live_pass, diagnose_items
                from src.ingest.razorpay_source import fetch_payments
                items, diagnosis = diagnose_items(items, fetch_payments())
                res = live_pass(items, now, cfg, path=path,
                           limit=arg("--max", None, int), sup_path=SUPPRESSION_P,
                           notif_path=NOTIFICATIONS_P, promise_path=PROMISES_P,
                           deliver=deliver, wf_cfg=wf_cfg, gateway=gateway)
            else:
                res = run_pass(items, now, cfg, dry_run=not execute, path=path,
                           limit=arg("--max", None, int),
                           sup_path=SUPPRESSION_P,
                           notif_path=NOTIFICATIONS_P,
                           promise_path=PROMISES_P,
                           deliver=deliver, wf_cfg=wf_cfg,
                           gateway=gateway)
    except runlock.LockHeld as e:
        raise SystemExit(f"refusing to run: {e}")
    if res.halted:
        print(f"  RUN HALTED     {res.halted}")
    print(f"  opened          {len(res.opened)} new sequences")
    if res.advanced:
        print(f"  advanced        {len(res.advanced)} attempt(s) this pass")
        for a in res.advanced:
            print(f"    {a['reference'][:26]:<26} {a['channel']:<22} "
                  f"{a['status']:<12} {a.get('execution_url') or a['detail'][:60]}")
    print(f"  declined        {len(res.declined_to_open)} below the exposure floor "
          f"({rs(sum(d['amount_paise'] for d in res.declined_to_open))})")
    print()

    by_class: dict[str, list] = {}
    for e in L.read(path):
        if e["event"] == L.SEQUENCE_OPENED:
            by_class.setdefault(e.get("decline_class") or "?", []).append(e)
    print("CAMPAIGNS BY DECLINE CLASS")
    for k, v in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        amt = sum(int(e["amount_paise"]) for e in v)
        print(f"  {k:<18} {len(v):>3} sequences   {rs(amt):>14}")
    print()

    if deliver:
        # WHO WOULD BE MESSAGED, named before anything is sent. The channel is
        # decided per case from the identity we hold, so this table is also the
        # only place the "no contact on this case" population is visible -- and
        # that population is the majority of this book.
        from src.recovery.campaign import rebuild
        rows = []
        for ref in sorted(L.open_sequences(path)):
            seq = rebuild(ref, cfg, path)
            if seq is None:
                continue
            want = delivery_for(seq, deliver, wf_cfg)
            via = "+".join(k for k, v in want.items() if v)
            to, real = contact_for(seq, wf_cfg)
            # A PREVIEW MUST NOT PROMISE MORE THAN THE ENGINE WILL DO. The
            # opt-out list is checked by `authorize_contact` at send time, so
            # a suppressed customer was never going to be messaged -- but this
            # table listed them as reachable, which is exactly backwards for
            # something whose whole job is to be read before pressing send.
            if via and suppression.is_suppressed(seq.customer_ref, SUPPRESSION_P):
                via, opted = "", True
            else:
                opted = False
            rows.append((ref, real, via, seq.at_risk_paise, opted, to))
        reachable = [r for r in rows if r[2]]
        muted_optout = [r for r in rows if r[4]]
        print(f"DELIVERY  {len(reachable)} of {len(rows)} open campaigns can "
              f"be messaged")
        redirected = any(r[5] != r[1] for r in reachable)
        if redirected:
            print("    (delivery is REDIRECTED -- the case's own customer is "
                  "never messaged)")
            print(f"    {'case':<24} {'for':<22} {'ACTUALLY GOES TO':<28} "
                  f"{'via':<6} {'amount':>10}")
            for ref, who, via, amt, _, to in reachable[:14]:
                print(f"    {ref[:24]:<24} {str(who)[:22]:<22} {to:<28} "
                      f"{via:<6} {rs(amt):>10}")
        else:
            for ref, who, via, amt, _, to in reachable[:12]:
                print(f"    {ref[:26]:<26} {str(who):<26} {via:<7} "
                      f"{rs(amt):>12}")
        shown = 14 if redirected else 12
        if len(reachable) > shown:
            print(f"    ... {len(reachable)-shown} more")
        if redirected:
            tally: dict[str, int] = {}
            for r in reachable:
                tally[r[5]] = tally.get(r[5], 0) + 1
            print()
            print("    messages per recipient:")
            for who, k in sorted(tally.items(), key=lambda kv: -kv[1]):
                print(f"      {who:<30}{k:>4}")
        if muted_optout:
            print(f"    {len(muted_optout)} campaign(s) held a contact who has "
                  f"opted out. Not messaged:")
            for _, who, _, _, _, _ in muted_optout:
                print(f"      {who}")
        mute = len(rows) - len(reachable) - len(muted_optout)
        if mute:
            print(f"    {mute} campaign(s) hold no contact -- a link is still "
                  f"created, nobody is messaged.")
            print("    Razorpay orders carry no e-mail or phone. Asking for "
                  "SMS on those would")
            print("    have texted the placeholder customer the API call "
                  "needs.")
        print()

    if horizon:
        print(f"CAMPAIGN PLAYED FORWARD  ({horizon} days, one pass per hour)")
        fired: list[dict] = []
        stopped: list[dict] = []
        deferred = 0
        # A DRY RUN MEANS "TOUCH NOTHING OUTSIDE". For the Razorpay merchant
        # that means creating no links, so the horizon can only ever show what
        # WOULD fire. A local book has no outside: the gateway writes to our
        # own files and mints nothing that costs anything, so the horizon can
        # execute for real and show what the ladder actually recovers. The
        # ledger is still the scratch one either way.
        local_book = m.gateway != "razorpay"
        for h in range(1, horizon * 24 + 1):
            t = now + timedelta(hours=h)
            if gateway is not None:
                gateway.clock = t
            r = run_pass(items, t, cfg, dry_run=True, path=path,
                         sup_path=SUPPRESSION_P, notif_path=NOTIFICATIONS_P,
                         promise_path=PROMISES_P, gateway=gateway,
                         wf_cfg=wf_cfg, deliver=deliver)
            for a in r.advanced:
                a["hour"] = h
                fired.append(a)
            for s in r.stopped:
                s["hour"] = h
                stopped.append(s)
            deferred += len(r.deferred)

        print(f"  {len(fired)} attempts fired, {deferred} deferred out of quiet "
              f"hours, {len(stopped)} sequences closed")
        won = [e for e in L.read(path) if e["event"] == L.ATTEMPT_SUCCEEDED]
        silent = [a for a in fired if a["channel"] == "silent"]
        if silent:
            captured = [a for a in silent if a["status"] == "SUCCEEDED"]
            print(f"  {len(silent)} silent re-presents, {len(captured)} "
                  f"captured, 0 customers contacted by any of them")
        if won:
            print(f"  RECOVERED  {rs(sum(int(e.get('amount_paise') or 0) for e in won))} "
                  f"across {len({e['reference'] for e in won})} subscription(s)")
        print()
        print(f"  {'day':>5}  {'reference':<24} {'attempt':<8} {'channel':<22} "
              f"{'class':<16} {'status'}")
        for a in fired[:28]:
            print(f"  {a['hour']/24:>5.1f}  {a['reference'][:24]:<24} "
                  f"{a['attempt_no']+1:<8} {a['channel']:<22} "
                  f"{a['decline_class']:<16} {a['status']}")
        if len(fired) > 28:
            print(f"  ... {len(fired)-28} more")
        print()

        why: dict[str, int] = {}
        for s in stopped:
            why[s["reason"]] = why.get(s["reason"], 0) + 1
        print("  SEQUENCES CLOSED, AND WHY  (stopping rules)")
        for k, v in sorted(why.items(), key=lambda kv: -kv[1]):
            amt = sum(s["amount_paise"] for s in stopped if s["reason"] == k)
            print(f"    {v:>4}  {k:<34} {rs(amt):>14}")
        print()

        chan: dict[str, int] = {}
        for a in fired:
            chan[a["channel"]] = chan.get(a["channel"], 0) + 1
        print("  ATTEMPTS BY CHANNEL")
        for k, v in sorted(chan.items(), key=lambda kv: -kv[1]):
            print(f"    {v:>4}  {k}")
        print()

    print(f"MEASURED RECOVERED  {rs(L.recovered_paise(m.ledger))}  "
          f"(real ledger, read back from the API)")
    print(f"ledger  {path}  ({len(L.read(path))} events)")


if __name__ == "__main__":
    main()
