"""The four workflows, and what makes each one different.

Every stream runs the same loop -- detect, diagnose, choose, recover -- and the
loop is only worth separating because the first two steps genuinely differ:

  payment_degradation   detected STATISTICALLY, across many payments. No single
                        failure is evidence; a rate shift is. Diagnosed to a
                        rail, and fixed by moving traffic rather than by asking
                        a customer for anything.

  checkout_abandonment  detected by ABSENCE. Nothing failed, nothing was
                        reported, and there is nothing to diagnose.

  subscription_failure  detected PER SUBSCRIPTION, and uniquely retryable in
                        silence, because a mandate is standing consent.

  invoice_overdue       detected by ARITHMETIC on a date. Nothing happens to
                        make a receivable overdue; time passes.

What is deliberately NOT here: the bounds. Quiet hours, the opt-out list, the
contact ceilings, the exposure floor and the stopping rules live in
`declines.yaml` and apply to every workflow identically. A workflow chooses
what to do. It never chooses whether the limits apply, and there is no hook
here through which it could.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .channels import load_workflows

# The revenue-at-risk `kind` each workflow owns.
KIND = {
    "payment_degradation": "payment_failure",
    "checkout_abandonment": "checkout_abandoned",
    "subscription_failure": "subscription_failure",
    "invoice_overdue": "overdue_receivable",
}
BY_KIND = {v: k for k, v in KIND.items()}

ORDER = ["payment_degradation", "checkout_abandonment",
         "subscription_failure", "invoice_overdue"]


@dataclass(frozen=True)
class Workflow:
    key: str
    title: str
    plain: str
    detect: str
    diagnose: str
    unit: str
    schedule: str
    kind: str
    requires: list[str] = field(default_factory=list)
    prefers_rail_change: bool = False
    mandate_backed: bool = False
    ends_with_human: bool = False
    promise_to_pay: bool = False
    min_volume_per_segment: int = 0
    # A hard, observed constraint on THIS account, as opposed to a general
    # requirement. Kept separate so a reviewer can tell "this needs something
    # you could go and enable" from "this needs something no account has".
    account_blocked: str = ""

    @property
    def diagnoses(self) -> bool:
        """Does this workflow have anything to diagnose at all?

        Two of the four do not, and saying so is more useful than pretending
        every stream runs the same machinery. An abandoned cart has no fault to
        find; an overdue invoice has no fault either, only a debtor.
        """
        return self.key in ("payment_degradation", "subscription_failure")


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def load(cfg: dict | None = None) -> dict[str, Workflow]:
    cfg = cfg or load_workflows()
    out: dict[str, Workflow] = {}
    for key, spec in (cfg.get("workflows") or {}).items():
        out[key] = Workflow(
            key=key,
            title=spec.get("title", key.replace("_", " ").title()),
            plain=spec.get("plain", ""),
            detect=_clean(spec.get("detect")),
            diagnose=_clean(spec.get("diagnose")),
            unit=spec.get("unit", "case"),
            schedule=spec.get("schedule", "abandoned"),
            kind=KIND.get(key, key),
            requires=[_clean(r) for r in (spec.get("requires") or [])],
            prefers_rail_change=bool(spec.get("prefers_rail_change", False)),
            mandate_backed=bool(spec.get("mandate_backed", False)),
            ends_with_human=bool(spec.get("ends_with_human", False)),
            promise_to_pay=bool(spec.get("promise_to_pay", False)),
            min_volume_per_segment=int(spec.get("min_volume_per_segment", 0)),
            account_blocked=_clean(spec.get("account_blocked", "")),
        )
    return out


def for_kind(kind: str, cfg: dict | None = None) -> Workflow | None:
    return load(cfg).get(BY_KIND.get(kind, ""))


def ordered(cfg: dict | None = None) -> list[Workflow]:
    wf = load(cfg)
    return [wf[k] for k in ORDER if k in wf]


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------

@dataclass
class Readiness:
    """Whether a workflow can actually run here, and what is missing.

    Reported per workflow rather than as one global caveat, because the
    blockers differ: payment degradation needs VOLUME, subscription failure
    needs a MANDATE, and the other two need nothing at all. Collapsing them
    into a single "some things are simulated" footnote loses the only detail a
    reviewer can act on.
    """
    workflow: str
    ready: bool
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def readiness(wf: Workflow, items: list, gateway_caps=None,
              sources: dict | None = None, caps_for: dict | None = None
              ) -> Readiness:
    """Can this loop run, and on what.

    `sources` maps a workflow key to the book actually answering for it --
    "live" or "local". A loop reading a book this project holds is RUNNING; it
    is not blocked, and calling it blocked was the mistake that made two
    working loops look like defects. What it is not is live, and the note says
    so every time rather than once in a footnote.
    """
    blockers: list[str] = []
    notes: list[str] = []
    mine = [i for i in items if i.kind == wf.kind]
    src = (sources or {}).get(wf.key)
    # THE CAPABILITIES OF THE GATEWAY THAT SERVES THIS BOOK, which is not
    # necessarily the merchant's. The subscription book is served by the local
    # gateway and that one holds mandates; reporting the Razorpay account's
    # missing mandate against it says something false about a loop that is
    # visibly executing silent rungs.
    caps = (caps_for or {}).get(wf.key, gateway_caps)

    if src == "local":
        notes.append(
            "reading a book this project holds. The live account is tried "
            "first every time and answers the moment it can")

    if wf.min_volume_per_segment and src != "local":
        by_seg: dict[str, int] = {}
        for i in mine:
            by_seg[i.segment] = by_seg.get(i.segment, 0) + 1
        best = max(by_seg.values(), default=0)
        if best < wf.min_volume_per_segment:
            blockers.append(
                f"needs {wf.min_volume_per_segment} payments in one segment to "
                f"tell a rate shift from noise; the largest here has {best}")

    # An account-level gate stops the LIVE path, not the loop. Once a local
    # book is answering, repeating it as a blocker says something false.
    if wf.account_blocked:
        (notes if src == "local" else blockers).append(
            wf.account_blocked + (" -- reading the local book instead"
                                  if src == "local" else ""))

    if wf.mandate_backed:
        can = bool(caps and caps.can_charge_saved_instrument)
        if not can:
            blockers.append(
                "needs an active mandate, e-mandate or UPI Autopay "
                "authorisation; this account holds none, so every silent rung "
                "is reported unexecutable rather than modelled")
        else:
            notes.append("mandate present: the silent rungs are free")

    if not mine:
        notes.append("no cases on this account right now")

    return Readiness(workflow=wf.key, ready=not blockers,
                     blockers=blockers, notes=notes)


def ladder_shape(wf: Workflow, declines_cfg: dict,
                 wf_cfg: dict | None = None) -> dict:
    """What this workflow's ladder actually looks like, for reporting.

    Counts CONTACTS separately from attempts, because that is the number a
    compliance ceiling binds and the number a cost estimate multiplies.
    """
    from .channels import get as get_channel
    spec = declines_cfg["schedules"][wf.schedule]
    contacting = set(declines_cfg["compliance"]["contacting_channels"])
    steps = spec["steps"]
    chans = [s["channel"] for s in steps]
    contacts = [c for c in chans if c in contacting]
    silent = [c for c in chans if c == "silent"]
    return {
        "schedule": wf.schedule,
        "steps": len(steps),
        "contacts": len(contacts),
        "silent": len(silent),
        "span_days": round(max(s["after_hours"] for s in steps) / 24, 1),
        "channels": chans,
        "cost_paise": sum(get_channel(c, wf_cfg).cost_paise for c in chans),
        "ends_with": chans[-1] if chans else None,
    }
