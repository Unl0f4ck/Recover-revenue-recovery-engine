"""Driving the sequencer against the live account.

`dunning.py` is the state machine and knows nothing about Razorpay. This module
is the seam: it reads what is at risk, rebuilds live sequences from the ledger,
advances whichever have a step due, and reconciles outcomes back from the API.

REBUILT, NOT REMEMBERED. A campaign runs over days; the process that opened a
sequence on Monday is long gone by Thursday. So every sequence is reconstructed
from its own ledger events, and nothing depends on in-memory state surviving.
This is the same lesson the first live batch taught -- recovery links outlive
the process that created them, so measurement has to reconcile against the
account rather than trust what a variable said.

The practical consequence is that this file can be run repeatedly, on a cron,
from a cold start, and it converges to the same place: each pass fires only what
is due, and a step that has already been taken is visible in the ledger and is
not taken twice.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..execution import razorpay as rz
from ..gateways import get_gateway
from . import channels, notify, promises
from ..ingest.razorpay_source import RevenueAtRisk
from . import dunning as D
from . import ledger as L
from ..policy import load_policy
from .declines import (Classification, classify, classify_abandonment,
                       classify_subscription,
                       classify_receivable, load_declines)

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class CampaignResult:
    ran_at: datetime
    opened: list[str] = field(default_factory=list)
    advanced: list[dict] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)
    stopped: list[dict] = field(default_factory=list)
    declined_to_open: list[dict] = field(default_factory=list)
    at_risk_paise: int = 0
    recovered_paise: int = 0
    spent_paise: int = 0                 # exposure touched by this run
    halted: str = ""                     # why the run stopped early, if it did

    @property
    def live_sequences(self) -> int:
        return len(self.opened) + len(self.advanced)


# ---------------------------------------------------------------------------
# REBUILDING
# ---------------------------------------------------------------------------

def rebuild(reference: str, cfg: dict, path: Path | None = None
            ) -> D.Sequence | None:
    """Reconstruct a sequence from its ledger events. Returns None if never opened.

    Everything the plan depends on -- the decline reason, the amount, the kind,
    the moment the clock started -- was written into the SEQUENCE_OPENED event
    for exactly this purpose. Replanning from those inputs reproduces the same
    steps with the same due dates, because `plan` is a pure function of them.
    """
    events = L.events_for(reference, path)
    opened = next((e for e in events if e["event"] == L.SEQUENCE_OPENED), None)
    if opened is None:
        return None
    extra = opened.get("extra") or {}
    kind = extra.get("kind", "payment_failure")
    reason = extra.get("reason")
    # Must mirror `classify_item` exactly. It did not: `subscription_failure`
    # fell through to the reason-based classifier, so a subscription opened on
    # the 7-step `mandate` ladder was rebuilt on the 5-step `slow` one -- a
    # different plan, different due dates, and four silent rungs gone. The
    # docstring above promises replanning reproduces the same steps, and for
    # one kind it quietly did not.
    cls = (classify_abandonment(cfg) if kind == "checkout_abandoned"
           else classify_receivable(cfg) if kind == "overdue_receivable"
           else classify_subscription(reason, cfg)
           if kind == "subscription_failure"
           else classify(reason, cfg))
    seq = D.plan(reference, kind, int(opened.get("amount_paise", 0)), cls,
                 datetime.fromisoformat(opened["at"]), cfg,
                 failing_method=extra.get("failing_method"),
                 diagnosis_family=extra.get("diagnosis_family"),
                 customer_ref=extra.get("customer_ref"))
    # Whether THIS case has a standing mandate is a fact recorded at open time,
    # not something to re-derive. See `_can_charge`.
    seq.extra["mandate"] = bool(extra.get("mandate"))
    seq.extra.update(extra)
    # A policy edit must never rewrite a campaign's historical contract.
    seq.policy_version = opened["policy_version"]
    seq.sequence_id = opened["sequence_id"]
    if extra.get("classification"):
        seq.classification = Classification(**extra["classification"])
    if extra.get("plan"):
        seq.steps = [D.Step(**{**s, "due_at": datetime.fromisoformat(s["due_at"])})
                     for s in extra["plan"]]
    else:
        scheduled = [e for e in events if e["event"] == L.ATTEMPT_SCHEDULED]
        if scheduled:
            seq.steps = [D.Step(int(e["attempt_no"]),
                datetime.fromisoformat(e["detail"].removeprefix("due ")),
                e["channel"], e.get("rail") or "none",
                e["channel"] == "silent") for e in scheduled]
    return seq


def classify_item(item: RevenueAtRisk, cfg: dict) -> Classification:
    if item.kind == "checkout_abandoned":
        return classify_abandonment(cfg)
    if item.kind == "overdue_receivable":
        return classify_receivable(cfg)
    if item.kind == "subscription_failure":
        return classify_subscription((item.detail or {}).get("error_reason"),
                                     cfg)
    return classify((item.detail or {}).get("error_reason"), cfg)


# ---------------------------------------------------------------------------
# OPENING
# ---------------------------------------------------------------------------

def open_new(items: list[RevenueAtRisk], now: datetime, cfg: dict,
             res: CampaignResult, path: Path | None = None) -> None:
    """Open a sequence for anything at risk that does not already have one.

    Idempotent by construction: an existing SEQUENCE_OPENED event means we skip,
    so re-running this on the same book neither duplicates campaigns nor resets
    anyone's schedule back to day zero.
    """
    for item in items:
        res.at_risk_paise += item.amount_paise
        if L.events_for(item.reference, path):
            continue
        ok, why = D.may_open(item.amount_paise, cfg, item.kind)
        if not ok:
            res.declined_to_open.append(
                {"reference": item.reference, "amount_paise": item.amount_paise,
                 "reason": why})
            continue
        cls = classify_item(item, cfg)
        # THE CLOCK STARTS WHEN WE OPEN, not when the payment failed.
        #
        # Dating the schedule from the original failure looks more principled
        # and is wrong in both directions on a real book. A payment that failed
        # twenty days ago would open already past `max_sequence_days` and stop
        # without a single attempt; one that failed five days ago would have its
        # first three steps simultaneously due. Dunning begins when the system
        # sees the failure, which is what every prior-art system does. The
        # original failure time is kept in the ledger for the audit trail.
        seq = D.plan(item.reference, item.kind, item.amount_paise, cls,
                     now, cfg, failing_method=item.method,
                     diagnosis_family=(item.detail or {}).get("diagnosis_family"),
                     customer_ref=(item.detail or {}).get("customer_ref"))
        seq.extra["failed_at"] = item.created_at.isoformat()
        seq.extra["customer_ref"] = seq.customer_ref
        seq.extra["mandate"] = bool((item.detail or {}).get("mandate"))
        seq.extra["order_id"] = (item.detail or {}).get("order_id")
        seq.extra["segment"] = item.segment
        seq.extra["source_state"] = (item.detail or {}).get("status", "failed")
        seq.diagnosis_family = (item.detail or {}).get("diagnosis_family")
        seq.extra["diagnosis"] = (item.detail or {}).get("diagnosis")
        D.open_sequence(seq, path)
        res.opened.append(item.reference)


# ---------------------------------------------------------------------------
# EXECUTION
# ---------------------------------------------------------------------------

def _link_description(seq: D.Sequence, step: D.Step) -> str:
    what = {"checkout_abandoned": "your incomplete checkout",
            "overdue_receivable": "your overdue invoice",
            }.get(seq.kind, "your payment that did not go through")
    return (f"Recovery for {what} [{seq.reference}] "
            f"step {step.attempt_no + 1} / {len(seq.steps)}")


def execute_step(seq: D.Sequence, step: D.Step, now: datetime, cfg: dict,
                 dry_run: bool = True, env: dict | None = None,
                 observed_state: str | None = None,
                 deliver: dict | None = None,
                 gateway=None, wf_cfg: dict | None = None) -> D.AttemptOutcome:
    """Perform one step. Never called without the gate and the clock agreeing.

    The channel-to-execution mapping is deliberately conservative about what
    this account can honestly do:

      silent            needs a saved token or e-mandate. We have neither, so
                        this reports UNEXECUTABLE rather than pretending.
      reconcile         reads the gateway. Never charges, never contacts.
      payment_link      a REAL test-mode Payment Link.
      alternate_*       the same, with the failing rail recorded as excluded --
                        intent only; Payment Links cannot enforce it (SPEC 11).
      payment_update    also a link. Razorpay has no card-update token flow, but
                        a fresh link lets the customer choose a new instrument,
                        which reaches the same end by a different route. Named
                        for what it does, not for what getpaidhq calls it.
    """
    # ASK THE GATEWAY, do not assume. This read `if step.requires_mandate:
    # return UNEXECUTABLE` unconditionally, which hard-coded one Razorpay
    # account's missing entitlement as a universal truth about payments -- the
    # exact thing `Capabilities` exists to prevent, in the words of its own
    # docstring. Razorpay supports tokens, e-mandate and UPI Autopay; THIS
    # account holds none, and that is a fact about the account.
    #
    # It matters beyond tidiness: the silent rungs are the cheapest and least
    # intrusive in the whole system, and docs/RECOVERY_RATE.md identifies the
    # mandate as the binding constraint on recovery rate. A merchant who has
    # one should get those rungs, and until now no merchant could.
    if step.requires_mandate:
        if not _can_charge(gateway, env, seq):
            return D.AttemptOutcome(
                "UNEXECUTABLE",
                "requires a saved token or e-mandate; this case has none"
                if not seq.extra.get("mandate") else
                "requires a saved token or e-mandate; this gateway cannot "
                "charge one")
        gw = gateway or get_gateway("razorpay", env=env)
        return _charge_mandate(seq, step, gw, dry_run)

    if step.channel == "reconcile":
        if observed_state in ("failed", "created", "attempted", "pending", "halted", "issued"):
            return D.AttemptOutcome("FAILED", f"confirmed unpaid: {observed_state}",
                                    execution_mode="SIMULATED" if dry_run else "REAL")
        if dry_run:
            return D.AttemptOutcome("FAILED", "preview: assuming still unpaid")
        return D.AttemptOutcome(
            "AMBIGUOUS", f"reconciled; gateway state {observed_state!r}",
            execution_mode="REAL" if observed_state else "SIMULATED")

    if step.channel in ("payment_link", "alternate_method_link", "payment_update"):
        exclude = D.exclude_methods(seq, step)
        if dry_run:
            return D.AttemptOutcome(
                "DELIVERED",
                f"dry run: would create a link excluding {exclude or 'nothing'}")
        try:
            gw = gateway or get_gateway("razorpay", env=env)
            art = gw.create_recovery(
                seq.at_risk_paise, _link_description(seq, step),
                exclude_methods=exclude, notify=deliver,
                customer=_customer_of(seq, wf_cfg),
                notes={"reference": seq.reference,
                       "attempt": str(step.attempt_no),
                       "idempotency_key": L.idempotency_key(
                           seq.reference, step.attempt_no, seq.policy_version)})
            r = art
        except Exception as e:                       # noqa: BLE001
            # Transport-level, so AMBIGUOUS -- this is the #16292 path, and it
            # deliberately does NOT advance the sequence.
            return D.AttemptOutcome("AMBIGUOUS", f"transport failure: {e}"[:300])
        # DELIVERED, not SUCCEEDED. The link exists and the customer has it; no
        # money has moved. Whether it does is settled later by
        # `reconcile_links`, reading the API. Conflating "we contacted them"
        # with "we recovered it" is the single easiest way to report a recovery
        # rate that is really a send rate.
        return D.AttemptOutcome(
            "DELIVERED", r.detail, execution_mode=r.mode,
            execution_reference=r.reference, execution_url=r.url)

    # A channel with no executor. This fallback existed but was stranded after
    # a `return` inside `_customer_of`, so it was dead code and this function
    # fell off the end returning None -- which `advance` would have read as
    # `None.status`. Nothing reaches it today because `human_review` and
    # `write_off` are caught by `terminal_channels` in `check_stop` first, so
    # it was a latent crash rather than a live one. It is still the difference
    # between adding a channel to the config and getting a clear answer, and
    # adding one and getting an AttributeError.
    return D.AttemptOutcome("UNEXECUTABLE",
                            f"no executor for channel {step.channel!r}")


def redirect_target(seq: D.Sequence, wf_cfg: dict | None = None) -> str | None:
    """Where this case's message actually goes, if delivery is redirected.

    Deterministic in the case reference, so the same case always reaches the
    same person and a re-run does not reshuffle who saw what. Spread across the
    configured contacts rather than piling everything on the first one.

    The channel preference follows what we know about the real customer: a case
    that holds an e-mail redirects to an e-mail, one that holds a phone number
    redirects to a phone. A case that holds NEITHER can now be delivered too --
    the reason those were muted is that the placeholder in the API call is not
    a person who agreed to hear from us, and a redirect target is exactly that
    person. Those spread across both channels so both get exercised.
    """
    wf_cfg = wf_cfg if wf_cfg is not None else channels.load_workflows()
    r = (channels.delivery(wf_cfg).get("redirect") or {})
    if not r.get("enabled"):
        return None
    emails = [str(x) for x in (r.get("email") or [])]
    phones = [str(x) for x in (r.get("sms") or [])]

    ref = (seq.customer_ref or "").strip()
    if ref and "@" in ref:
        pool = emails or phones
    elif ref:
        pool = phones or emails
    else:
        pool = emails + phones
    if not pool:
        return None
    n = int(hashlib.sha256(seq.reference.encode()).hexdigest(), 16)
    return pool[n % len(pool)]


def contact_for(seq: D.Sequence, wf_cfg: dict | None = None
                ) -> tuple[str | None, str | None]:
    """(who we will message, who the case is really for).

    Both, always, so no caller has to remember which one it is holding. The
    second value is what the ledger and the suppression list use; the first is
    the only thing that reaches Razorpay.
    """
    real = (seq.customer_ref or "").strip() or None
    to = redirect_target(seq, wf_cfg)
    return (to or real), real


def delivery_for(seq: D.Sequence, deliver: dict | None,
                 wf_cfg: dict | None = None) -> dict:
    """Which delivery channels to request FOR THIS CASE.

    Derived from the identity we actually hold, not from a run-wide setting,
    and this is a safety rule rather than a nicety.

    `create_recovery_link` substitutes a placeholder customer when we pass none,
    so the API call succeeds on an object that carries no contact details --
    and 37 of the live campaigns are Razorpay orders, which carry none. A
    run-wide `{"sms": True}` would therefore have asked Razorpay to text a
    placeholder mobile number on every one of them. It is a real-format Indian
    number and it is not ours.

    So: an e-mail address gets e-mail, a phone number gets SMS, and a case with
    neither gets nothing. Asking for SMS on a case where we only know an
    address is the same mistake in a quieter form.
    """
    want = {"sms": False, "email": False}
    if not deliver:
        return want
    to, _ = contact_for(seq, wf_cfg)
    if not to:
        return want
    if "@" in to:
        want["email"] = bool(deliver.get("email"))
    else:
        want["sms"] = bool(deliver.get("sms"))
    return want


def _can_charge(gateway, env: dict | None = None,
                seq: D.Sequence | None = None) -> bool:
    """Can we silently re-present THIS case? Two conditions, both required.

    The provider must support charging a stored instrument, and this
    particular case must actually have one. Checking only the first was a real
    error and an expensive-looking one: `fast` and `slow` -- the ordinary
    one-off failure ladders -- each open with a `requires_mandate` silent rung
    too, not just the 7-step `mandate` schedule. So a gateway that could charge
    mandates handed a free silent retry to every SOFT_FUNDS and SOFT_TECHNICAL
    checkout failure in the book, which is precisely the "recovering money the
    real account could not have" that the simulated gateway exists to avoid.

    A merchant can have Subscriptions enabled and still take one-off payments
    with no stored instrument. The capability is the provider's; the mandate is
    the customer's.
    """
    if seq is not None and not seq.extra.get("mandate"):
        return False
    try:
        gw = gateway or get_gateway("razorpay", env=env)
        return bool(gw.capabilities().can_charge_saved_instrument)
    except Exception:                                    # noqa: BLE001
        # No gateway available is not a licence to assume a capability.
        return False


def _charge_mandate(seq: D.Sequence, step: D.Step, gw,
                    dry_run: bool) -> D.AttemptOutcome:
    """A silent re-present against a standing mandate.

    SILENT IS THE POINT. No message is sent, nobody is interrupted, and the
    contact ceiling is untouched -- which is why a subscription book can run a
    long retry series where a checkout book gets three messages and stops.
    """
    if dry_run:
        return D.AttemptOutcome(
            "DELIVERED", "dry run: would re-present against the saved mandate")
    try:
        art = gw.charge_mandate(seq.at_risk_paise, seq.reference,
                                attempt_no=step.attempt_no)
    except Exception as e:                               # noqa: BLE001
        # Same rule as a link: a transport failure is AMBIGUOUS, never a
        # failure, because we do not know whether the charge landed.
        return D.AttemptOutcome("AMBIGUOUS", f"transport failure: {e}"[:300])
    if art.paid:
        return D.AttemptOutcome(
            "SUCCEEDED", f"silent re-present captured {art.reference}",
            execution_mode=art.mode, execution_reference=art.reference,
            amount_paise=art.amount_paid_paise or art.amount_paise)
    # A declined re-present is a real decline, not an ambiguous one: the
    # gateway answered.
    return D.AttemptOutcome(
        "FAILED", f"silent re-present declined: {art.detail}"[:300],
        execution_mode=art.mode, execution_reference=art.reference)


def _customer_of(seq: D.Sequence, wf_cfg: dict | None = None) -> dict | None:
    """Address the message to the person who will actually receive it."""
    to, _ = contact_for(seq, wf_cfg)
    if not to:
        return None
    return {"email": to} if "@" in to else {"contact": to}


# ---------------------------------------------------------------------------
# ADVANCING
# ---------------------------------------------------------------------------

def advance(seq: D.Sequence, now: datetime, cfg: dict, res: CampaignResult,
            dry_run: bool = True, env: dict | None = None,
            observed_state: str | None = None, kill_switch: bool = False,
            path: Path | None = None, sup_path: Path | None = None,
            notif_path: Path | None = None, deliver: dict | None = None,
            gateway=None, wf_cfg: dict | None = None,
            promise_path: Path | None = None) -> None:
    """Move one sequence forward by at most one step.

    Order is load-bearing. Stop conditions are checked BEFORE anything is due,
    so a sequence whose payment has already been captured, cancelled or expired
    never sends another message (#16398). Only then do we ask whether a step is
    due, and only then whether contacting the customer is permitted right now.
    """
    reason = D.check_stop(seq, now, observed_state, cfg, path, kill_switch)
    if reason:
        D.stop(seq, reason, now, path=path)
        res.stopped.append({"reference": seq.reference, "reason": reason,
                            "amount_paise": seq.at_risk_paise})
        return

    step = D.due_step(seq, now, cfg, path)
    if step is None:
        return

    decision = D.authorize_contact(seq, step, now, cfg, path, sup_path,
                                   notif_path, wf_cfg, promise_path)
    if not decision.allowed:
        if decision.paused:
            # A promise is neither a deferral nor a stop. The ladder simply
            # does not advance while it stands, and resumes from where it was.
            res.deferred.append({"reference": seq.reference,
                                 "until": None, "reason": decision.reason})
            return
        if decision.defer_until is not None:
            D.defer(seq, step, decision.defer_until, decision.reason, now, path)
            res.deferred.append({"reference": seq.reference,
                                 "until": decision.defer_until.isoformat(),
                                 "reason": decision.reason})
        else:
            why = decision.terminal or D.STOP_CONTACT_CEILING
            # A message we chose not to send is still a fact about this
            # customer, and it is the fact a complaint investigation needs.
            notify.record(seq.reference, seq.sequence_id, step.attempt_no,
                          "none", notify.SUPPRESSED, now,
                          customer_ref=seq.customer_ref,
                          detail=decision.reason, path=notif_path)
            D.stop(seq, why, now, decision.reason, path)
            res.stopped.append({"reference": seq.reference,
                                "reason": why,
                                "detail": decision.reason,
                                "amount_paise": seq.at_risk_paise})
        return

    # A channel we cannot actually operate -- a phone call with no telephony
    # provider. Recorded as a step the ladder WOULD take, never as one taken.
    if not decision.executable:
        D.record_unexecutable(seq, step, now, path)
        res.advanced.append({
            "reference": seq.reference, "attempt_no": step.attempt_no,
            "channel": step.channel, "rail": step.rail,
            "decline_class": seq.classification.decline_class,
            "status": "UNEXECUTABLE", "detail": decision.reason,
            "execution_mode": "SIMULATED", "execution_url": None,
            "amount_paise": seq.at_risk_paise})
        return

    # Skip a step this account cannot perform before writing anything ahead:
    # an in-flight record asserts that a call was made, and none was.
    #
    # Capability-driven, like `execute_step`: a gateway that CAN charge a
    # stored instrument proceeds to the write-ahead and the call, and only one
    # that cannot skips the rung.
    if step.requires_mandate and not _can_charge(gateway, env, seq):
        D.record_unexecutable(seq, step, now, path)
        res.advanced.append({
            "reference": seq.reference, "attempt_no": step.attempt_no,
            "channel": step.channel, "rail": step.rail,
            "decline_class": seq.classification.decline_class,
            "status": "UNEXECUTABLE",
            "detail": "requires a saved token or e-mandate; skipped",
            "execution_mode": "SIMULATED", "execution_url": None,
            "amount_paise": seq.at_risk_paise})
        return

    # DUPLICATE SUPPRESSION. A replayed webhook, an overlapping cron or a
    # retried pass must not send the same rung's message twice. The idempotency
    # key stops the provider double-charging; this stops the customer being
    # messaged twice for one attempt.
    contacting = step.channel in cfg["compliance"]["contacting_channels"]
    want = delivery_for(seq, deliver, wf_cfg)
    channels = [c for c, on in want.items() if on] or ["none"]
    gap = timedelta(hours=float(
        cfg["compliance"].get("min_hours_between_attempts", 4)))
    if contacting and all(
            notify.already_sent(seq.reference, step.attempt_no, c, notif_path,
                                since=now - gap)
            for c in channels):
        return

    D.write_ahead(seq, step, now, path)
    # WHO IT WAS FOR, AND WHERE IT WENT. `customer_ref` stays the case's real
    # customer -- that is what suppression keys on and what an investigation
    # asks about -- so under a redirect the ledger recorded a message to
    # someone who never received it, and for the many cases whose customer is
    # unknown it recorded `null` while a message was genuinely sent. Neither is
    # a usable audit trail. The recipient goes in the detail line beside it.
    to, _real = contact_for(seq, wf_cfg)
    went_to = f" -> {to}" if to and to != seq.customer_ref else ""
    if contacting:
        for c in channels:
            notify.record(seq.reference, seq.sequence_id, step.attempt_no, c,
                          notify.REQUESTED, now, customer_ref=seq.customer_ref,
                          detail=f"{step.channel} via {c}{went_to}",
                          path=notif_path)
    out = execute_step(seq, step, now, cfg, dry_run, env, observed_state,
                       want, gateway, wf_cfg)
    if out.status == "UNEXECUTABLE":
        D.record_unexecutable(seq, step, now, path)
    else:
        D.record_outcome(seq, step, out, now, path)
    if contacting:
        # STOPS AT REQUESTED ON PURPOSE. Razorpay sends the message itself and
        # does not report per-message delivery back to us, so promoting this to
        # SENT because the API returned 200 would be inventing a fact. A
        # provider with delivery webhooks would advance it further; this one
        # cannot, and the ledger says so.
        for c in channels:
            notify.record(
                seq.reference, seq.sequence_id, step.attempt_no, c,
                notify.FAILED if out.status == "AMBIGUOUS" else notify.REQUESTED,
                now, customer_ref=seq.customer_ref,
                artefact=out.execution_reference,
                detail=out.detail[:160], path=notif_path)

    res.spent_paise += seq.at_risk_paise
    res.advanced.append({
        "reference": seq.reference, "attempt_no": step.attempt_no,
        "channel": step.channel, "rail": step.rail,
        "decline_class": seq.classification.decline_class,
        "status": out.status, "detail": out.detail,
        "execution_mode": out.execution_mode, "execution_url": out.execution_url,
        "amount_paise": seq.at_risk_paise})


# ---------------------------------------------------------------------------
# THE PASS
# ---------------------------------------------------------------------------

def run_pass(items: list[RevenueAtRisk], now: datetime | None = None,
             cfg: dict | None = None, dry_run: bool = True,
             env: dict | None = None, states: dict[str, str] | None = None,
             kill_switch: bool | None = None, path: Path | None = None,
             limit: int | None = None, sup_path: Path | None = None,
             policy: dict | None = None, notif_path: Path | None = None,
             deliver: dict | None = None, gateway=None,
             wf_cfg: dict | None = None,
             promise_path: Path | None = None,
             blocked_refs: set[str] | None = None) -> CampaignResult:
    """One sweep: open what is new, advance what is due, stop what is finished.

    Designed to be run on a schedule. Each pass is small -- most sequences have
    nothing due -- and the ledger is the only thing that carries state between
    passes.
    """
    cfg = cfg or load_declines()
    wf_cfg = wf_cfg if wf_cfg is not None else channels.load_workflows()
    policy = policy if policy is not None else load_policy()
    now = now or datetime.now(IST)
    states = states or {}
    res = CampaignResult(ran_at=now)
    bounds = policy.get("bounds", {})
    batch = policy.get("batch_limits", {})

    # THE KILL SWITCH IS NOW READ, rather than merely accepted as an argument.
    #
    # `bounds.global_kill_switch` has been in policy.yaml since day one and was
    # threaded through `advance` and `check_stop` as a parameter -- but nothing
    # ever read the config into it, so the production path defaulted it to
    # False forever. A safety control that is documented, plumbed and dead is
    # worse than one never written, because everyone downstream believes it
    # works. recoup makes the same control first-class: a flagged merchant
    # cannot emit a charge on ANY code path.
    if kill_switch is None:
        kill_switch = bool(bounds.get("global_kill_switch", False))
    if kill_switch:
        res.halted = "global_kill_switch is set in policy.yaml"
        return res

    open_new(items, now, cfg, res, path)
    item_states = {i.reference: (i.detail or {}).get("status") for i in items}
    # Explicit caller state wins. Absence from a truncated feed proves nothing.
    states = {**item_states, **states}

    # Per-run ceilings. These were read only by the OLD one-shot batch runner,
    # so the sequencer -- the path that now matters -- ran uncapped. Etherlabs
    # carries a budget on every decision record for the same reason: the point
    # of a batch limit is that one runaway pass cannot touch the whole book.
    max_actions = int(batch.get("max_interventions_per_run", 50))
    if limit is not None:
        max_actions = min(max_actions, limit)
    max_spend = int(batch.get("max_exposure_per_run_paise", 100_000_000))

    for ref in sorted(L.open_sequences(path)):
        if ref in (blocked_refs or set()):
            continue
        seq = rebuild(ref, cfg, path)
        if seq is None:
            continue
        reason = D.check_stop(seq, now, states.get(ref), cfg, path, kill_switch)
        if reason:
            D.stop(seq, reason, now, path=path)
            res.stopped.append({"reference": ref, "reason": reason,
                                "amount_paise": seq.at_risk_paise})
            continue
        # Budgets limit interventions, never the discovery of a stop condition
        # on later references in this same sweep.
        if len(res.advanced) >= max_actions:
            res.halted = f"per-run intervention cap reached ({len(res.advanced)}/{max_actions})"
            continue
        if res.spent_paise >= max_spend:
            res.halted = f"per-run exposure cap reached ({res.spent_paise}/{max_spend} paise)"
            continue
        if res.spent_paise + seq.at_risk_paise > max_spend:
            res.deferred.append({"reference": ref, "until": None,
                                "reason": "would exceed per-run exposure cap"})
            continue
        advance(seq, now, cfg, res, dry_run, env, states.get(ref),
                kill_switch, path, sup_path, notif_path, deliver, gateway,
                wf_cfg, promise_path)

    res.recovered_paise = L.recovered_paise(path)
    return res


# ---------------------------------------------------------------------------
# MEASUREMENT
# ---------------------------------------------------------------------------

def reconcile_links(now: datetime | None = None, env: dict | None = None,
                    path: Path | None = None, gateway=None,
                    cfg: dict | None = None) -> int:
    """Read every link this campaign created and record the ones that were paid.

    MEASURED, not modelled. A link that was paid is recovered revenue; one that
    was not, is not. Reading it back from the provider rather than from our own
    optimism is the entire difference between a recovery number and a forecast.

    Runs against the ledger, so it works from a cold start on links created by
    a process that exited days ago.

    GOES THROUGH THE GATEWAY, which it did not used to. This function called
    `rz.fetch_payment_link` directly, so the one path in the system that decides
    what counts as recovered revenue was the one path that ignored the provider
    abstraction it was built on -- `fetch_recovery` on the Razorpay adapter had
    been doing exactly this call all along. Routing it through the gateway costs
    nothing on the live account and means any other provider, including the
    simulated one, is measured by identical code rather than by a second
    implementation that could drift into being kinder to itself.
    """
    now = now or datetime.now(IST)
    cfg = cfg or load_declines()
    gw = gateway or get_gateway("razorpay", env=env or rz.load_env())
    found = 0
    seen: set[str] = set()
    for e in L.read(path):
        ref, link = e.get("reference"), e.get("execution_reference")
        if not link or not ref or link in seen:
            continue
        seen.add(link)
        if L.has_succeeded(ref, path):
            continue
        try:
            art = gw.fetch_recovery(link)
        except Exception:                            # noqa: BLE001
            continue
        if not art.paid:
            continue
        # A provider that reports a link as paid without an explicit paid
        # amount means it for the full amount. Losing that fallback would
        # record a genuine recovery as zero rupees.
        paid = art.amount_paid_paise or art.amount_paise
        seq = rebuild(ref, cfg, path)
        if seq is None:
            continue
        step = seq.steps[min(int(e.get("attempt_no", 0)), len(seq.steps) - 1)]
        D.record_outcome(seq, step, D.AttemptOutcome(
            "SUCCEEDED", f"payment link {link} paid", execution_mode=art.mode,
            execution_reference=link, execution_url=art.url,
            amount_paise=paid), now, path)
        D.stop(seq, D.STOP_RECOVERED, now, f"link {link} paid", path)
        found += 1
    return found
