"""Play a synthetic batch forward through the real engine.

THE LOOP IS THE WHOLE ARGUMENT, so it is worth being explicit about what each
tick does and, more importantly, what it does not do.

    1.  run_pass(...)        the PRODUCTION sweep. Opens what is new, advances
                             what is due, stops what is finished. Every guard --
                             quiet hours, contact ceilings, opt-out, promise
                             pauses, per-run caps, the kill switch -- runs here,
                             untouched.
    2.  react to contacts    the ONLY place this file decides anything about a
                             customer, and it decides one thing: what they did
                             about a message that was already sent.
    3.  settle payments      a customer who decided to pay pays some hours
                             later, so the link changes state on a LATER tick
                             than the one that created it.
    4.  reconcile_links(...) the PRODUCTION measurement pass, pointed at the
                             simulated gateway. Recovered money is discovered by
                             reading the provider back, never asserted.

Step 2 never writes to the ledger and never calls the sequencer. An opt-out
goes into the suppression file and is then discovered by `authorize_contact` on
a later tick exactly as a real one would be; a promise goes into the promise
file and pauses the ladder through `promises.holds`. The simulation is not
allowed to stop a campaign -- it can only do the things a customer can do, and
the engine decides what those mean.

WHY THE CLOCK TICKS RATHER THAN JUMPS. Schedules here are measured in hours (1,
4, 6, 24, 72, 168...), quiet hours are a property of the hour of day, and TRAI's
voice window is narrower still. An event-driven clock that jumped to the next
due time would have to re-implement all of that to know where to jump, and
would then be testing its own reimplementation. An hourly tick asks the engine
the same question 24 times a day and lets it answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from src.ingest.razorpay_source import RevenueAtRisk
from src.recovery import channels, ledger as L, promises, runlock, suppression
from src.recovery.campaign import IST, reconcile_links, run_pass
from src.recovery.declines import load_declines
from src.policy import load_policy
from src.sim import book as B
from src.sim import customer as C
from src.sim.gateway import SimGateway

OUT = Path("data/sim")


@dataclass(frozen=True)
class SimPaths:
    """Where one batch writes.

    INJECTABLE, because the test suite kept destroying the artifact. Every
    batch calls `fresh()`, which truncates these files -- so the four small
    batches in `tests/test_sim.py` overwrote whatever a real run had produced,
    and reading `data/sim/ledger.jsonl` after `pytest` gave you a fifteen-case
    test fixture wearing the filename of a two-hundred-case result. That is a
    quiet way to publish a wrong number, and it nearly did.
    """
    root: Path = OUT

    @property
    def ledger(self) -> Path: return self.root / "ledger.jsonl"

    @property
    def suppression(self) -> Path: return self.root / "suppression.jsonl"

    @property
    def notifications(self) -> Path: return self.root / "notifications.jsonl"

    @property
    def promises(self) -> Path: return self.root / "promises.jsonl"

    @property
    def lock(self) -> Path: return self.root / "run.lock"

    @property
    def all(self) -> tuple[Path, ...]:
        return (self.ledger, self.suppression, self.notifications,
                self.promises)


# The batch asks for delivery on every channel, and `campaign.delivery_for`
# narrows it per case: an e-mail address gets e-mail, a phone number gets SMS.
# Nothing is sent -- `SimGateway.create_recovery` records what was requested
# and dispatches nothing -- but the notification ledger now carries the channel
# each contact would have used, which is what makes the batch's cost figure a
# count of MESSAGES rather than a count of ladder steps.
DELIVER = {"sms": True, "email": True}

DEFAULT = SimPaths()

# Kept as module constants so the report and older callers still read the
# default batch without knowing about SimPaths.
LEDGER = DEFAULT.ledger
SUPPRESSION = DEFAULT.suppression
NOTIFICATIONS = DEFAULT.notifications
PROMISES = DEFAULT.promises
PATHS = DEFAULT.all

# The simulated batch takes the SAME run lock the live runner takes, against
# its own lock file. Two batches sharing one root would otherwise not merely
# interleave: `fresh()` deletes the file the other is reading, and the other
# dies parsing a half-written line. That is not a hypothetical, it is how the
# first full-size run of this script ended.
#
# The live path already had this exact protection for the same reason. A
# simulator that can corrupt its own results is not a cheaper way to test the
# system, it is a second system to debug.
LOCK = DEFAULT.lock


@dataclass
class BatchResult:
    """Everything the report needs, and nothing it has to recompute."""
    started: datetime
    ended: datetime
    seed: int
    p_base: float
    items: list[RevenueAtRisk] = field(default_factory=list)
    personas: dict[str, C.Persona] = field(default_factory=dict)
    gateway: SimGateway | None = None
    ticks: int = 0
    contacts: int = 0
    reactions: dict[str, int] = field(default_factory=dict)
    opted_out: list[dict] = field(default_factory=list)
    promised: list[dict] = field(default_factory=list)
    promises_kept: int = 0
    promises_broken: int = 0
    deferrals: dict[str, int] = field(default_factory=dict)
    declined_to_open: list[dict] = field(default_factory=list)
    halted_passes: dict[str, int] = field(default_factory=dict)
    paths: SimPaths = DEFAULT

    @property
    def at_risk_paise(self) -> int:
        return sum(i.amount_paise for i in self.items)


def fresh(paths=None) -> None:
    """A batch starts from nothing.

    Deliberately NOT append-only, unlike every other ledger in this project.
    These files are output, not record: a simulated batch that accumulated
    across runs would report the sum of every experiment ever run as though it
    were one campaign. The live ledger under `data/live/` is never touched by
    anything in this package.
    """
    for p in (paths if paths is not None else DEFAULT.all):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.unlink(missing_ok=True)


def _react_to_new_contacts(gw: SimGateway, seen: int, now: datetime,
                           res: BatchResult, rng, wf_cfg: dict) -> int:
    """Every link created this tick is a message that reached somebody."""
    links = list(gw.links.values())
    for link in links[seen:]:
        ref = (link.notes or {}).get("reference")
        persona = res.personas.get(ref)
        if persona is None:
            continue
        res.contacts += 1
        r = C.react(persona, now, link.amount_paise, res.p_base, rng)
        res.reactions[r.kind] = res.reactions.get(r.kind, 0) + 1

        if r.kind == C.PAYS:
            persona.extra.setdefault("pays", []).append((r.at, link.reference))
        elif r.kind == C.OPTS_OUT:
            # Suppress the PERSON, not the case. The same distinction the reply
            # reader had to learn: an opt-out is about a human being and
            # applies to every campaign they appear in, now and later.
            who = persona.extra.get("customer_ref")
            if who:
                suppression.suppress(
                    who, now, reason=f"simulated reply: {r.detail}",
                    action=suppression.OPT_OUT, source="sim-customer",
                    path=res.paths.suppression)
                res.opted_out.append({"reference": ref, "customer_ref": who,
                                      "at": now, "detail": r.detail})
        elif r.kind == C.PROMISES:
            try:
                promises.record(ref, r.at, now, amount_paise=link.amount_paise,
                                channel="reply", note=r.detail,
                                recorded_by="sim-customer", cfg=wf_cfg,
                                path=res.paths.promises)
            except promises.PromiseRefused:
                # The engine refused it -- too far out, or too many already
                # broken. That is a rule doing its job, not a simulation error.
                res.reactions["promise_refused"] = \
                    res.reactions.get("promise_refused", 0) + 1
            else:
                persona.extra.setdefault("promise_due", []).append(r.at)
                res.promised.append({"reference": ref, "pay_by": r.at,
                                     "at": now, "detail": r.detail})
    return len(links)


def _settle(gw: SimGateway, now: datetime, res: BatchResult) -> None:
    """Customers who decided to pay, and whose moment has arrived, pay."""
    for persona in res.personas.values():
        due = persona.extra.get("pays") or []
        keep = []
        for when, link_ref in due:
            if when <= now:
                gw.mark_paid(link_ref, when)
            else:
                keep.append((when, link_ref))
        if due:
            persona.extra["pays"] = keep


def _resolve_promises(gw: SimGateway, now: datetime, res: BatchResult) -> None:
    """A promised date arrives. They either pay or they do not.

    A broken promise is left to expire on its own. The engine resumes the
    ladder once `promises.holds` stops holding, and letting that happen by
    itself is the point -- a simulation that reached in and restarted the
    sequence would be testing its own code rather than the engine's.
    """
    for ref, persona in res.personas.items():
        dues = persona.extra.get("promise_due") or []
        keep = []
        for when in dues:
            if when > now:
                keep.append(when)
                continue
            if persona.will_keep_promise:
                link = _latest_link(gw, ref)
                if link is not None:
                    gw.mark_paid(link, when)
                res.promises_kept += 1
            else:
                res.promises_broken += 1
        persona.extra["promise_due"] = keep


def _latest_link(gw: SimGateway, reference: str) -> str | None:
    for link in reversed(list(gw.links.values())):
        if (link.notes or {}).get("reference") == reference:
            return link.reference
    return None


def run(n: int = 240, days: int = 21, seed: int = 20260829,
        start: datetime | None = None, tick_hours: int = 1,
        p_base: float | None = None, limit: int | None = None,
        progress=None, out_dir: Path | None = None) -> BatchResult:
    """One batch, start to finish. Reproducible from `seed` alone."""
    cfg = load_declines()
    wf_cfg = channels.load_workflows()
    policy = load_policy()
    rng = np.random.default_rng(seed)

    if p_base is None:
        # The fitted per-contact conversion rate, taken from the analysis rather
        # than chosen here. See src/sim/customer.py.
        from scripts.recovery_curve import BOOK_A, NO_AUTOMATION, fit_p
        p_base = fit_p(NO_AUTOMATION[0], BOOK_A)

    start = start or datetime(2026, 9, 1, 9, 0, tzinfo=IST)
    # Half the window, so even the last arrival has time to run its ladder
    # before the horizon. Cases still in flight at the end are reported as
    # still in flight rather than quietly counted as failures.
    items = B.generate(n, start, seed=seed, cfg=cfg,
                       arrival_days=days * 0.5)

    paths = SimPaths(Path(out_dir)) if out_dir else DEFAULT
    res = BatchResult(started=start, ended=start, seed=seed, p_base=p_base,
                      items=items, paths=paths)
    from src.recovery.campaign import classify_item
    for it in items:
        cls = classify_item(it, cfg).decline_class
        p = C.draw_persona(it.reference, cls, rng)
        p.extra["customer_ref"] = (it.detail or {}).get("customer_ref")
        p.extra["kind"] = it.kind
        res.personas[it.reference] = p

    # A MERCHANT WITH SUBSCRIPTIONS ENABLED. This is the one capability the
    # simulated gateway is allowed to have that the live Razorpay account does
    # not, and it is deliberate rather than convenient: without it every silent
    # rung reports UNEXECUTABLE and the subscription loop demonstrates nothing
    # but its own absence. It only ever affects steps marked
    # `requires_mandate`, which only the `mandate` schedule has, which only
    # subscription cases are routed to -- so no other stream can recover money
    # the real account could not have.
    amount_of = {i.reference: i.amount_paise for i in items}

    def _mandate_outcome(reference: str, attempt_no: int) -> bool:
        persona = res.personas.get(reference)
        if persona is None:
            return False
        return C.mandate_charge(persona, amount_of.get(reference, 0),
                                res.p_base, rng)

    gw = SimGateway(mandate=True, mandate_outcome=_mandate_outcome)
    res.gateway = gw

    with runlock.exclusive(path=paths.lock, label="simulate_batch"):
        _play(gw, items, res, cfg, wf_cfg, policy, rng, start, days,
              tick_hours, limit, progress)
    return res


def _play(gw, items, res, cfg, wf_cfg, policy, rng, start, days,
          tick_hours, limit, progress) -> None:
    """The tick loop, held inside the run lock."""
    fresh(res.paths.all)
    _declined: dict[str, dict] = {}
    seen_links = 0
    now = start
    end = start + timedelta(days=days)
    while now <= end:
        gw.clock = now
        # THE BOOK ARRIVES OVER TIME. Feeding the engine a case before it has
        # happened would open a campaign against a payment that has not failed
        # yet -- and, less obviously, would collapse every ladder onto the same
        # hour of the day. See src/sim/book.py.
        visible = [i for i in items if i.created_at <= now]
        pass_res = run_pass(
            visible, now, cfg, dry_run=False, env={},
            path=res.paths.ledger, sup_path=res.paths.suppression,
            notif_path=res.paths.notifications,
            promise_path=res.paths.promises, gateway=gw, wf_cfg=wf_cfg,
            policy=policy, limit=limit, deliver=DELIVER)

        if pass_res.halted:
            key = pass_res.halted.split("(")[0].strip()
            res.halted_passes[key] = res.halted_passes.get(key, 0) + 1
        for d in pass_res.deferred:
            why = str(d.get("reason", "")).split("(")[0].strip()[:60]
            res.deferrals[why] = res.deferrals.get(why, 0) + 1
        # ACCUMULATED, not taken from the first pass. The book arrives over
        # time, so a pass only sees the cases that exist yet -- keeping the
        # first pass's list undercounted every case that arrived later and
        # fell below its floor, and the report then disagreed with itself:
        # 227 "chased" against 215 actually opened.
        for d in pass_res.declined_to_open:
            if d["reference"] not in _declined:
                _declined[d["reference"]] = d
        res.declined_to_open = list(_declined.values())

        seen_links = _react_to_new_contacts(gw, seen_links, now, res, rng,
                                            wf_cfg)
        _resolve_promises(gw, now, res)
        _settle(gw, now, res)
        reconcile_links(now=now, path=res.paths.ledger, gateway=gw)

        res.ticks += 1
        res.ended = now
        if progress is not None:
            progress(res, now)
        now += timedelta(hours=tick_hours)


def recovered_paise(paths: SimPaths | None = None) -> int:
    """Read back from the ledger, by the same function the live runner uses."""
    return L.recovered_paise((paths or DEFAULT).ledger)
