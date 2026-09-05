"""Measured money recovered across a batch, with the audit trail behind it.

    python -m scripts.simulate_batch                  # 240 cases, 21 days
    python -m scripts.simulate_batch --n 500 --days 30
    python -m scripts.simulate_batch --seed 7         # a different book
    python -m scripts.simulate_batch --case sim_inv_0042XXXXXX   # one ladder

WHAT IS SIMULATED, STATED ONCE AND PLAINLY. Two things: which cases exist, and
whether a contacted customer pays. Everything between them is the production
engine -- the same schedules, the same guards, the same append-only ledger, the
same reconcile. See src/sim/__init__.py.

SO THE RECOVERY RATE HERE IS NOT A MEASUREMENT. It is a consequence of the
per-contact conversion assumption in src/sim/customer.py, which comes from
fitting one free parameter to a published third-party band. Feed in a different
assumption and a different rate comes out; that is what an assumption is. The
only measured recovery in this project is the Rs 28,433 on the live Razorpay
test account, read back from the API.

WHAT THE BATCH DOES ESTABLISH is everything the assumption does not touch, and
it is the part a single live case cannot show at all:

    - the ladder terminates, on every one of hundreds of cases
    - no customer is contacted after asking to stop
    - no message is sent outside the hours that channel permits
    - no case exceeds its contact ceiling
    - every attempt written ahead has a recorded outcome
    - the money arithmetic survives the cost of working the whole book

Those are properties of the engine. They are as true on a synthetic book as on
a real one, and the INVARIANTS section below verifies each of them against the
ledger rather than asserting it.
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timedelta

from src.eval.bootstrap import wilson_interval
from src.recovery import channels, ledger as L, notify, suppression, view
from src.recovery.campaign import rebuild
from src.recovery.declines import load_declines
from src.recovery.workflows import for_kind
from src.sim import run as R


# The funnel labels contain an em dash, and a Windows console defaulting to
# cp1252 renders it as a replacement character mid-report. Reconfiguring the
# stream is less brittle than removing the character and hoping nobody adds
# another.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def rs(paise) -> str:
    return f"Rs {paise/100:,.0f}"


def arg(name: str, default=None, cast=str):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return default


def rule(title: str) -> None:
    print()
    print(title)
    print("-" * max(len(title), 44))


# ---------------------------------------------------------------------------
# INVARIANTS -- checked against the ledger, not asserted
# ---------------------------------------------------------------------------

def check_no_contact_after_opt_out(res) -> tuple[bool, str]:
    """The one that would be a regulatory problem, not a quality problem."""
    stops = {}
    for row in suppression.read(res.paths.suppression):
        who = suppression.normalise(row.get("customer_ref"))
        at = datetime.fromisoformat(row["at"])
        stops[who] = min(at, stops.get(who, at))
    if not stops:
        return True, "nobody opted out in this batch"

    bad = 0
    for n in notify.read(res.paths.notifications):
        if n.get("status") == notify.SUPPRESSED:
            continue                       # a message we chose NOT to send
        who = suppression.normalise(n.get("customer_ref"))
        when = stops.get(who)
        if when and datetime.fromisoformat(n["at"]) > when:
            bad += 1
    return bad == 0, (f"{len(stops)} opted out; {bad} message(s) after"
                      if bad else
                      f"{len(stops)} opted out, 0 messages to any of them after")


def check_quiet_hours(res, wf_cfg: dict) -> tuple[bool, str]:
    """Every message, against the window its own channel permits.

    Per channel rather than globally, because voice is bound by TRAI's 09:00 -
    21:00 and messaging is not. A single global check would pass a call placed
    at 21:30 and prove nothing about the rule that actually binds.
    """
    bad = []
    for n in L.read(res.paths.ledger):
        # The LEDGER, not the notification log. A notification row's `detail`
        # is the provider's own words ("simulated link excluding nothing"),
        # so parsing a channel out of it produced five nonexistent channels
        # and a failing check that was measuring my string handling rather
        # than the engine's compliance. The ledger records the step channel
        # in a field, which is what a field is for.
        if n["event"] != L.ATTEMPT_DELIVERED:
            continue
        name = n.get("channel") or ""
        ch = channels.get(name, wf_cfg)
        if not ch.hours_ist:
            # A channel with no declared window is not silently exempt -- it is
            # a gap in the config, and counting it as a pass would make the
            # check weaker the more channels someone forgets to configure.
            bad.append((name or "?", -1))
            continue
        lo, hi = ch.hours_ist
        hour = datetime.fromisoformat(n["at"]).hour
        if not (lo <= hour < hi):
            bad.append((name, hour))
    if bad:
        c = Counter(bad)
        return False, "; ".join(
            (f"{k[0]} has no declared window x{v}" if k[1] < 0
             else f"{k[0]} at {k[1]:02d}:00 x{v}") for k, v in c.items())
    return True, "every message inside its own channel's permitted hours"


def check_contact_ceiling(res, cfg: dict) -> tuple[bool, str]:
    cap = int(cfg["compliance"]["max_contacts_per_reference"])
    contacting = cfg["compliance"]["contacting_channels"]
    over = {}
    for ref in {e["reference"] for e in L.read(res.paths.ledger)}:
        n = L.contacts_made(ref, contacting, res.paths.ledger)
        if n > cap:
            over[ref] = n
    return not over, (f"cap is {cap} per case; worst case in the batch is "
                      f"{max((L.contacts_made(r, contacting, res.paths.ledger) for r in {e['reference'] for e in L.read(res.paths.ledger)}), default=0)}"
                      if not over else f"{len(over)} case(s) over the cap")


def check_write_ahead(res) -> tuple[bool, str]:
    """Every in-flight record must be answered.

    An `attempt_inflight` with no outcome is the shape of a process that died
    mid-call, and on a real gateway it is the shape of a possible double
    charge. On a batch of hundreds it is the cheapest integrity check there is.
    """
    rows = L.read(res.paths.ledger)
    inflight = Counter((e["reference"], e.get("attempt_no")) for e in rows
                       if e["event"] == L.ATTEMPT_INFLIGHT)
    answered = Counter((e["reference"], e.get("attempt_no")) for e in rows
                       if e["event"] in (L.ATTEMPT_SUCCEEDED, L.ATTEMPT_DELIVERED,
                                         L.ATTEMPT_AMBIGUOUS, L.ATTEMPT_FAILED,
                                         L.ATTEMPT_UNEXECUTABLE))
    orphans = [k for k, v in inflight.items() if answered.get(k, 0) < 1]
    return not orphans, (f"{sum(inflight.values())} attempts written ahead, "
                         f"{len(orphans)} without a recorded outcome")


def check_one_success(res) -> tuple[bool, str]:
    wins = Counter(e["reference"] for e in L.read(res.paths.ledger)
                   if e["event"] == L.ATTEMPT_SUCCEEDED)
    dupes = {k: v for k, v in wins.items() if v > 1}
    return not dupes, (f"{len(wins)} case(s) recovered, none counted twice"
                       if not dupes else f"{len(dupes)} counted more than once")


# ---------------------------------------------------------------------------

def across_seeds(n: int, days: int, seeds: list[int]) -> None:
    """The same book size, several draws. Print the spread, not a point.

    ONE SEED IS NOT A RESULT at this book size, and reporting it as one is the
    easiest way to publish a number that will not reproduce. Fifty abandoned
    cases against a 22% recoverability ceiling means the recovered count is a
    handful either way, and the seed-to-seed range on this stream runs from
    5.6% to 23.5% -- wider than the binomial interval from any single run,
    because the persona draw varies too, not just the conversions.

    The default seed happens to sit at the top of that range. Which is exactly
    why this exists.
    """
    from src.recovery import ledger as SL
    kinds = ("checkout_abandoned", "payment_failure", "overdue_receivable",
             "subscription_failure")
    rows: dict[str, list[float]] = {k: [] for k in kinds}
    print(f"ACROSS {len(seeds)} SEEDS  --  {n} cases, {days} days each")
    print()
    print(f"  {'seed':>10}" + "".join(f"{k.split('_')[0][:9]:>11}" for k in kinds))
    for seed in seeds:
        res = R.run(n=n, days=days, seed=seed,
                    out_dir=f"data/sim/seed_{seed}")
        led = SL.read(res.paths.ledger)
        won = {e["reference"] for e in led
               if e["event"] == SL.ATTEMPT_SUCCEEDED}
        opened = {e["reference"] for e in led
                  if e["event"] == SL.SEQUENCE_OPENED}
        line = f"  {seed:>10}"
        for k in kinds:
            refs = {i.reference for i in res.items if i.kind == k} & opened
            r = len(refs & won) / len(refs) if refs else 0.0
            rows[k].append(r)
            line += f"{r:>10.1%} "
        print(line)
    print()
    print(f"  {'range':>10}" + "".join(
        f"{min(rows[k]):>5.1%}-{max(rows[k]):<5.1%}" for k in kinds))
    print()
    print("  A single seed is one draw, not a measurement. The spread above")
    print("  is the honest width; the per-run report quotes one draw.")


def main() -> None:
    n = arg("--n", 240, int)
    days = arg("--days", 21, int)
    seed = arg("--seed", 20260829, int)
    cfg = load_declines()
    wf_cfg = channels.load_workflows()

    if "--seeds" in sys.argv:
        k = arg("--seeds", 4, int)
        base = arg("--seed", 20260829, int)
        return across_seeds(n, days, [base] + [base + i for i in range(1, k)])

    print("BATCH RECOVERY RUN  --  synthetic book, production engine")
    print(f"  book         {n} cases, seed {seed}")
    print(f"  window       {days} days, one pass per hour")
    print(f"  policy       {cfg['policy_version']}")
    print(f"  ledger       {R.LEDGER}   (data/live/ is never touched)")
    print()
    print("  SIMULATED    which cases exist; whether a contacted customer pays")
    print("  REAL         schedules, guards, ledger, reconcile -- every")
    print("               decision between those two is production code")

    res = R.run(n=n, days=days, seed=seed)
    now = res.ended + timedelta(hours=1)
    cs = view.campaigns(now, cfg, res.paths.ledger)

    # ---------------------------------------------------------------- book
    rule("THE BOOK")
    by_kind: dict[str, list] = {}
    for it in res.items:
        by_kind.setdefault(it.kind, []).append(it)
    for kind, group in sorted(by_kind.items(), key=lambda kv: -sum(
            i.amount_paise for i in kv[1])):
        amt = sum(i.amount_paise for i in group)
        print(f"  {kind:<22}{len(group):>5} cases  {rs(amt):>14}")
    print(f"  {'TOTAL AT RISK':<22}{len(res.items):>5} cases  "
          f"{rs(res.at_risk_paise):>14}")

    declined = sum(d["amount_paise"] for d in res.declined_to_open)
    if res.declined_to_open:
        print()
        print(f"  not chased            {len(res.declined_to_open):>5} cases  "
              f"{rs(declined):>14}   below the per-workflow exposure floor")
    chased = res.at_risk_paise - declined
    print(f"  chased                {len(res.items)-len(res.declined_to_open):>5} cases  "
          f"{rs(chased):>14}")

    # --------------------------------------------------------------- money
    rule("MONEY")
    recovered = R.recovered_paise(res.paths)
    won = [c for c in cs if c.recovered_paise > 0]
    opened = len(cs)
    lo, hi = wilson_interval(len(won), opened) if opened else (0.0, 0.0)

    chan_counts: Counter = Counter()
    kind_of = {i.reference: i.kind for i in res.items}
    nuisance = 0
    nuis_cfg = ((wf_cfg.get("costs") or {})
                .get("nuisance_paise_per_contact") or {})
    # A CONTACT IS A DELIVERY, and only a delivery. This counted
    # ATTEMPT_SUCCEEDED as well, so every recovered case was charged for one
    # extra interruption it never received -- the gap was exactly the 74
    # recoveries. A reconcile discovering that a link was paid is not a second
    # message to the customer; it is us reading the gateway.
    contacting_ch = set(cfg["compliance"]["contacting_channels"])
    for e in L.read(res.paths.ledger):
        if e["event"] != L.ATTEMPT_DELIVERED:
            continue
        ch = e.get("channel")
        if not ch or ch not in contacting_ch:
            continue
        chan_counts[ch] += 1
        # The price of one interruption, per workflow -- the same number the
        # exposure floors are derived from. Counting only the messaging would
        # report a return of nineteen thousand to one and invite exactly the
        # disbelief it deserves: the expensive part of a reminder has never
        # been the SMS.
        wf = for_kind(kind_of.get(e["reference"], ""), wf_cfg)
        if wf is not None:
            nuisance += int(nuis_cfg.get(wf.key, 0))

    # CASH IS COUNTED PER MESSAGE, from the notification ledger, not per ladder
    # step from the schedule. Before delivery was wired there was no channel to
    # count -- every notification row said "none" -- so the model charged 20p a
    # step and called every step an SMS. An e-mail is not 20p and a step that
    # reaches nobody is not a message at all.
    #
    # Deduplicated on (case, attempt, channel): `advance` records each contact
    # twice, once before the call and once after, and both rows are REQUESTED.
    sent: set[tuple] = set()
    msg_counts: Counter = Counter()
    for n in notify.read(res.paths.notifications):
        ch = n.get("channel")
        if ch in (None, "none") or n.get("status") == notify.SUPPRESSED:
            continue
        key = (n.get("reference"), n.get("attempt_no"), ch)
        if key in sent:
            continue
        sent.add(key)
        msg_counts[ch] += 1
    cash = sum(channels.delivery_cost(ch, wf_cfg) * k
               for ch, k in msg_counts.items())
    cost = cash + nuisance

    print(f"  recovered             {rs(recovered):>16}")
    print(f"  of revenue chased     {rs(chased):>16}"
          f"    {recovered/chased:.1%}" if chased else "")
    print(f"  of everything at risk {rs(res.at_risk_paise):>16}"
          f"    {recovered/res.at_risk_paise:.1%}")
    print()
    print(f"  cases recovered       {len(won)} of {opened} opened"
          f"    {len(won)/opened:.1%}  [95% CI {lo:.1%} - {hi:.1%}]"
          if opened else "  no sequences opened")
    print()
    n_contacts = sum(chan_counts.values())
    print(f"  contacts made         {n_contacts:>16}")
    detail = ", ".join(f"{k} x{v} @ {channels.delivery_cost(k, wf_cfg)}p"
                       for k, v in sorted(msg_counts.items())) or "nothing sent"
    print(f"  messages requested    {sum(msg_counts.values()):>16}    {detail}")
    print(f"  cash cost             {rs(cash):>16}")
    print(f"  cost of interrupting  {rs(nuisance):>16}    the price the exposure "
          f"floors are built on")
    print(f"  total cost            {rs(cost):>16}")
    print(f"  net contribution      {rs(recovered - cost):>16}")
    if cost:
        print(f"  return per rupee spent{recovered/cost:>15,.1f}x")
    print()
    print("  BY STREAM  -- the aggregate above is dominated by whichever")
    print("  stream carries the money, so it is broken out here.")
    print("  ONE DRAW. Abandonment in particular swings 5.6%-23.5% across")
    print("  seeds on a book this size; `--seeds 4` prints the spread.")
    won_refs = {c.reference: c.recovered_paise for c in cs}
    opened_refs = {c.reference for c in cs}
    print(f"    {'stream':<22}{'chased':>8}{'recovered':>14}{'rate':>8}"
          f"{'cases':>10}")
    for kind, group in sorted(by_kind.items()):
        refs = [i.reference for i in group if i.reference in opened_refs]
        amt = sum(i.amount_paise for i in group if i.reference in opened_refs)
        got = sum(won_refs.get(r, 0) for r in refs)
        k = sum(1 for r in refs if won_refs.get(r, 0) > 0)
        print(f"    {kind:<22}{rs(amt):>8}{rs(got):>14}"
              f"{(got/amt if amt else 0):>8.1%}{k:>5}/{len(refs):<4}")
    print()
    print("  The RATE is a consequence of the conversion assumption in")
    print("  src/sim/customer.py, not a measurement. The COST, the contact")
    print("  count and the arithmetic are properties of the engine.")

    # ------------------------------------------------------------- funnel
    rule("WHAT THE ENGINE DID")
    for label, count, note in view.funnel(cs, res.paths.ledger):
        print(f"  {label:<32}{count:>6}   {note}")

    rule("CAMPAIGN STATES")
    for state, count in view.state_counts(cs):
        print(f"  {state:<32}{count:>6}")

    rule("STOPPING RULES")
    # ONE ROW PER SEQUENCE, not one per stop event, because a sequence can
    # legitimately stop twice. A ladder that exhausts its rungs stops with
    # `terminal_channel_reached`, and if the customer then pays a link sent
    # days earlier, reconcile records the money and stops it again as
    # `recovered`. Both events are true and both belong in the ledger -- an
    # engine that refused the late payment because it had stopped chasing
    # would simply lose it. Counting events would double-count those cases.
    final: dict[str, str] = {}
    late: set[str] = set()
    for e in L.read(res.paths.ledger):
        if e["event"] != L.SEQUENCE_STOPPED:
            continue
        ref = e["reference"]
        reason = (e.get("extra") or {}).get("stop_reason") or "(unrecorded)"
        if ref in final and reason == "recovered":
            late.add(ref)
        final[ref] = reason
    # The AMOUNT, not just the count. Writing this up from the count alone,
    # I estimated the money at roughly twice what it turned out to be. A
    # figure worth citing is a figure worth computing.
    late_paise = sum(int(e.get("amount_paise") or 0)
                     for e in L.read(res.paths.ledger)
                     if e["event"] == L.ATTEMPT_SUCCEEDED
                     and e["reference"] in late)
    amounts = {c.reference: c.at_risk_paise for c in cs}
    for reason, count in Counter(final.values()).most_common():
        amt = sum(amounts.get(r, 0) for r, v in final.items() if v == reason)
        print(f"  {reason:<34}{count:>5}   {rs(amt):>14}")
    still = opened - len(final)
    if still > 0:
        print(f"  {'(still running at the horizon)':<34}{still:>5}")
    if late:
        print()
        print(f"  {len(late)} case(s), {rs(late_paise)}, paid a link AFTER the "
              f"ladder had stopped.")
        print("  Reconcile runs over the ledger, not over live sequences, so "
              "that money is")
        print("  still found. Chasing had stopped; measuring had not.")

    # ------------------------------------------------------- escalation
    rule("COMPLIANT ESCALATION")
    print(f"  contacts made                     {sum(chan_counts.values()):>5}")
    for ch, k in chan_counts.most_common():
        print(f"    {ch:<30}{k:>5}")
    print(f"  opted out mid-campaign            {len(res.opted_out):>5}"
          "   suppressed the person, not the case")
    print(f"  promises to pay recorded          {len(res.promised):>5}"
          f"   {res.promises_kept} kept, {res.promises_broken} broken")
    deferred = sum(res.deferrals.values())
    print(f"  contacts held, not dropped        {deferred:>5}")
    for why, k in sorted(res.deferrals.items(), key=lambda kv: -kv[1])[:4]:
        print(f"    {why[:44]:<44}{k:>5}")
    unexec = sum(1 for e in L.read(res.paths.ledger)
                 if e["event"] == L.ATTEMPT_UNEXECUTABLE)
    print(f"  planned but not performed         {unexec:>5}"
          "   no mandate, no telephony provider")
    if res.halted_passes:
        for why, k in res.halted_passes.items():
            print(f"  passes halted by a cap            {k:>5}   {why}")

    # ------------------------------------------------------- invariants
    rule("INVARIANTS  (verified against the ledger)")
    checks = [
        ("no contact after an opt-out", check_no_contact_after_opt_out(res)),
        ("no message outside permitted hours", check_quiet_hours(res, wf_cfg)),
        ("no case over its contact ceiling", check_contact_ceiling(res, cfg)),
        ("every attempt has an outcome", check_write_ahead(res)),
        ("no recovery counted twice", check_one_success(res)),
    ]
    failed = 0
    for label, (ok, detail) in checks:
        failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}]  {label:<36} {detail}")

    # ------------------------------------------------------ audit trail
    rule("AUDIT TRAIL")
    rows = L.read(res.paths.ledger)
    print(f"  {len(rows)} append-only events across {opened} campaigns")
    print(f"  {res.paths.ledger}")
    print()
    ref = arg("--case") or (won[0].reference if won else cs[0].reference)
    show_case(ref, now, cfg, res.paths)

    print()
    if failed:
        print(f"{failed} INVARIANT FAILED -- the batch is not compliant")
        raise SystemExit(1)
    print("All invariants hold across the batch.")


def show_case(ref: str, now: datetime, cfg: dict, paths) -> None:
    """One campaign, end to end. The audit trail is not a claim, it is a file."""
    seq = rebuild(ref, cfg, paths.ledger)
    if seq is None:
        print(f"  no campaign for {ref}")
        return
    events = L.events_for(ref, paths.ledger)
    print(f"  ONE CASE IN FULL  --  {ref}")
    print(f"    {seq.kind}, {rs(seq.at_risk_paise)}, "
          f"{seq.classification.decline_class} "
          f"-> schedule '{seq.classification.schedule}'")
    print()
    for t in view.timeline_of(seq, events, now, cfg, paths.ledger):
        when = t.at.strftime("%d %b %H:%M") if t.at else "            "
        # A future step is marked so the ladder reads as a plan with a part
        # that has not run yet, rather than as a list of things that happened.
        mark = "." if t.kind == "future" else " "
        print(f"   {mark} {when}  {t.label:<34}{(t.detail or '')[:56]}")


if __name__ == "__main__":
    main()
