"""Each workflow as a node graph, with live counts on every edge.

A recovery workflow is a pipeline, and a pipeline is much easier to judge as a
diagram than as prose: you can see at a glance where cases enter, where they
branch, which guard stopped them and where the money left. This builds that
graph from the SAME sources everything else reads -- the schedule config and the
append-only ledger -- so the picture cannot drift from the behaviour.

WHAT A NODE IS. One step the engine actually performs: a trigger, a detection, a
diagnosis, a guard, a rung of the ladder, a terminal outcome. Nothing decorative
and nothing aspirational -- if a node is on the canvas, there is code behind it
and a count from the ledger flowing through it.

WHAT AN EDGE CARRIES. How many cases took that path. That is the part a static
architecture diagram never has and the part that makes this worth drawing: a
guard with 20 cases down its reject edge is doing real work, and a rung with
zero throughput is a rung to delete.

Guard nodes are drawn on the main line rather than as side branches, because
that is what they are: every case passes through every guard, and only some
continue. Drawing them as optional detours would suggest a case could route
around one.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import ledger as L
from .channels import get as get_channel

# Node kinds, which drive shape and colour in the canvas.
TRIGGER = "trigger"
DETECT = "detect"
DIAGNOSE = "diagnose"
GUARD = "guard"
ACTION = "action"
WAIT = "wait"
RECONCILE = "reconcile"
WIN = "win"
STOP = "stop"


@dataclass
class Node:
    id: str
    kind: str
    title: str
    subtitle: str = ""
    detail: str = ""
    count: int = 0                 # cases that reached this node
    money_paise: int = 0
    state: str = "idle"            # idle | live | blocked | done
    note: str = ""


@dataclass
class Edge:
    src: str
    dst: str
    count: int = 0
    label: str = ""
    kind: str = "flow"             # flow | reject


@dataclass
class Graph:
    workflow: str
    title: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def add(self, n: Node) -> Node:
        self.nodes.append(n)
        return n

    def link(self, src: str, dst: str, count: int = 0, label: str = "",
             kind: str = "flow") -> None:
        self.edges.append(Edge(src, dst, count, label, kind))

    def as_dict(self) -> dict:
        return {"workflow": self.workflow, "title": self.title,
                "nodes": [asdict(n) for n in self.nodes],
                "edges": [asdict(e) for e in self.edges]}


def _counts(refs: set[str], path: Path | None) -> dict:
    """Ledger throughput for one workflow's references."""
    rows = [e for e in L.read(path) if e.get("reference") in refs]
    out = {
        "opened": sum(1 for e in rows if e["event"] == L.SEQUENCE_OPENED),
        "inflight": sum(1 for e in rows if e["event"] == L.ATTEMPT_INFLIGHT),
        "delivered": sum(1 for e in rows if e["event"] == L.ATTEMPT_DELIVERED),
        "deferred": len({(e["reference"], e["attempt_no"]) for e in rows
                         if e["event"] == L.CONTACT_DEFERRED}),
        "unexecutable": sum(1 for e in rows
                            if e["event"] == L.ATTEMPT_UNEXECUTABLE),
        "recovered": sum(1 for e in rows if e["event"] == L.ATTEMPT_SUCCEEDED),
        "recovered_paise": sum(int(e.get("amount_paise", 0)) for e in rows
                               if e["event"] == L.ATTEMPT_SUCCEEDED),
        "stopped": {},
        "by_channel": {},
    }
    for e in rows:
        if e["event"] == L.SEQUENCE_STOPPED:
            r = (e.get("extra") or {}).get("stop_reason", "unknown")
            out["stopped"][r] = out["stopped"].get(r, 0) + 1
        if e["event"] == L.ATTEMPT_INFLIGHT and e.get("channel"):
            c = e["channel"]
            out["by_channel"][c] = out["by_channel"].get(c, 0) + 1
    return out


_DETECT = {
    "payment_degradation": ("Watch the failure rate",
                            "binomial CUSUM, 5-minute cells"),
    "checkout_abandonment": ("Find orders nobody paid",
                             "created, unpaid, past the grace period"),
    "subscription_failure": ("Watch each subscription",
                             "a charge against a mandate failed"),
    "invoice_overdue": ("Check terms against the clock",
                        "issue date + terms + grace"),
}

_DIAGNOSE = {
    "payment_degradation": ("Find the failing rail",
                            "attribution ladder: issuer, PSP or network"),
    "subscription_failure": ("Read the decline reason",
                             "the reason decides the schedule"),
}


def build(wf, stream: dict, campaigns: list[dict], declines_cfg: dict,
          wf_cfg: dict | None = None, path: Path | None = None,
          degradation: dict | None = None) -> Graph:
    """One workflow's pipeline, with live throughput on every edge."""
    g = Graph(workflow=wf.key, title=wf.title)
    refs = set(stream.get("references") or [])
    c = _counts(refs, path)
    n_cases = stream.get("n", 0)
    declined = stream.get("declined_n", 0)

    # ---- trigger ----------------------------------------------------------
    # NAME THE BOOK THIS STREAM ACTUALLY READS. Hard-coding "Razorpay API" was
    # true for three of the four and false for the one that says, two panels
    # higher on the same screen, that it cannot reach that API at all.
    src = stream.get("loop", {}).get("source")
    if stream.get("recovered_mode") == "LOCAL" or src == "local":
        where = "a book this project holds, every pass"
    else:
        where = "Razorpay API, every pass"
    g.add(Node("trigger", TRIGGER, "Read the book" if "holds" in where
               else "Read the account", where,
               f"{n_cases + declined} object(s) looked at",
               count=n_cases + declined, state="live"))

    # ---- detect -----------------------------------------------------------
    dt, dsub = _DETECT.get(wf.key, ("Detect", ""))
    det = g.add(Node("detect", DETECT, dt, dsub, count=n_cases + declined,
                     state="live"))
    if wf.key == "payment_degradation" and degradation:
        det.detail = degradation.get("conclusion", "")
        det.note = degradation.get("thin_note", "")
        det.state = "live" if degradation.get("incident") else "idle"
        det.subtitle = (f"{dsub} · baseline "
                        f"{degradation.get('baseline_rate', 0):.0%}")
    g.link("trigger", "detect", n_cases + declined)

    prev = "detect"

    # ---- diagnose ---------------------------------------------------------
    if wf.diagnoses:
        dgt, dgsub = _DIAGNOSE.get(wf.key, ("Diagnose", ""))
        dg = g.add(Node("diagnose", DIAGNOSE, dgt, dgsub, count=n_cases,
                        state="live"))
        if wf.key == "payment_degradation" and degradation:
            if degradation.get("incident"):
                dg.detail = (f"{degradation.get('level')}: "
                             f"{degradation.get('cause')} "
                             f"({degradation.get('mechanism')})")
                dg.state = "live"
            else:
                dg.detail = "nothing detected, so nothing to diagnose"
                dg.state = "idle"
        g.link(prev, "diagnose", n_cases)
        prev = "diagnose"
    else:
        # Said out loud rather than omitted: an abandoned cart has no fault to
        # find, and a diagram that quietly skips the step implies one exists.
        nd = g.add(Node("nodiagnose", DIAGNOSE, "Nothing to diagnose",
                        "nothing failed" if wf.key == "checkout_abandonment"
                        else "the money is simply owed",
                        count=n_cases, state="idle"))
        g.link(prev, "nodiagnose", n_cases)
        prev = "nodiagnose"

    # ---- the exposure floor ------------------------------------------------
    from .economics import for_workflow
    ec = for_workflow(wf, declines_cfg, wf_cfg)
    floor = g.add(Node(
        "floor", GUARD, "Worth chasing?",
        f"below {_rs(ec.policy_floor_paise)} we decline",
        f"{n_cases} through, {declined} declined",
        count=n_cases + declined, state="live" if declined else "idle",
        note=(f"{_rs(ec.total_cost_paise)} of cash + {ec.contacts} interruptions "
              f"at {_rs(ec.nuisance_per_contact_paise)} each, divided by a "
              f"{ec.recovery_probability:.0%} recovery rate. Chasing only starts "
              f"LOSING money below {_rs(ec.cash_floor_paise)}; everything above "
              f"that is what we think it is worth not to bother someone. "
              + ec.policy_reason[:180])))
    g.link(prev, "floor", n_cases + declined)
    g.add(Node("toosmall", STOP, "Left alone",
               "too small to be worth a message",
               count=declined, money_paise=stream.get("declined_paise", 0),
               state="done" if declined else "idle"))
    g.link("floor", "toosmall", declined, "too small", "reject")

    # ---- the ladder --------------------------------------------------------
    steps = declines_cfg["schedules"][wf.schedule]["steps"]
    prev = "floor"
    running = n_cases
    for i, st in enumerate(steps):
        ch = get_channel(st["channel"], wf_cfg)
        used = c["by_channel"].get(st["channel"], 0)

        if i:
            w = g.add(Node(f"wait{i}", WAIT, _gap(steps, i), "",
                           count=running, state="idle"))
            g.link(prev, f"wait{i}", running)
            prev = f"wait{i}"

        if st["channel"] in ("human_review", "write_off"):
            # TERMINAL, so it hangs OFF the spine rather than sitting in it.
            # Drawn in-line it reads as a step the flow passes through on its
            # way to the next node, which is the opposite of what it is: the
            # ladder ends here for anyone who reaches it.
            n_end = c["stopped"].get("terminal_channel_reached", 0)
            g.add(Node(
                f"end{i}", STOP,
                "Hand to a person" if st["channel"] == "human_review"
                else "Give up",
                "somebody decides what happens next"
                if st["channel"] == "human_review"
                else "no sensible options left",
                count=n_end, state="done"))
            g.link(prev, f"end{i}", n_end, "nothing worked", "reject")
            break

        # The guard is added BEFORE the rung it gates, so the node list is
        # already in flow order and a renderer does not have to topologically
        # sort to draw a straight line.
        if ch.contacting and i == _first_contact(steps):
            gate = g.add(Node(
                "guards", GUARD, "May we contact them?",
                "opt-out · quiet hours · caps · promises",
                f"{c['deferred']} held back",
                count=running, state="live" if c["deferred"] else "idle",
                note="checked before every single message, in that order"))
            g.link(prev, "guards", running)
            g.add(Node("held", STOP, "Held for now",
                       "quiet hours, a cap, or a promise to pay",
                       count=c["deferred"],
                       state="done" if c["deferred"] else "idle"))
            g.link("guards", "held", c["deferred"], "not now", "reject")
            prev = "guards"

        silent = st["channel"] == "silent"
        blocked = silent and st.get("requires_mandate") and not _has_mandate(wf)
        node = g.add(Node(
            f"step{i}", ACTION, _say(st["channel"]),
            ("free, interrupts nobody" if silent else
             f"{ch.plain} · {_paise(ch.cost_paise)}"),
            f"{used} sent" if used else "",
            count=used,
            state="blocked" if blocked else ("live" if used else "idle"),
            note=("needs a saved mandate this account does not have"
                  if blocked else
                  (ch.unavailable_reason if not ch.executable else ""))))
        if not ch.executable and not silent:
            node.state = "blocked"

        g.link(prev, f"step{i}", used or running)
        prev = f"step{i}"

    # ---- reconcile and outcome ---------------------------------------------
    # Every contacting rung feeds the same reconciliation: a link is only a
    # recovery once the API says the money arrived.
    g.add(Node("reconcile", RECONCILE, "Ask Razorpay what happened",
               "a link sent is not money received",
               f"{c['delivered']} awaiting an answer",
               count=c["delivered"],
               state="live" if c["delivered"] else "idle"))
    fed = 0
    for i, st in enumerate(steps):
        if st["channel"] not in ("human_review", "write_off", "silent"):
            n = c["by_channel"].get(st["channel"], 0)
            g.link(f"step{i}", "reconcile", n)
            fed += n
    if not fed:
        g.link(prev, "reconcile", 0)

    g.add(Node("paid", WIN, "Paid", "confirmed by Razorpay",
               f"{_rs(c['recovered_paise'])} recovered",
               count=c["recovered"], money_paise=c["recovered_paise"],
               state="done" if c["recovered"] else "idle"))
    g.link("reconcile", "paid", c["recovered"], "money arrived")
    return g


# ---------------------------------------------------------------------------

def _rs(paise: int) -> str:
    return "Rs " + f"{round((paise or 0) / 100):,}"


def _paise(p: int) -> str:
    return f"{p} paise" if p < 100 else f"Rs {p/100:.2f}"


def _say(channel: str) -> str:
    return {"silent": "Retry quietly",
            "payment_link": "Send a payment link",
            "alternate_method_link": "Offer another way to pay",
            "payment_update": "Ask them to update their card",
            "voice": "Call them",
            "reconcile": "Check with Razorpay"}.get(channel, channel)


def _gap(steps: list[dict], i: int) -> str:
    hours = steps[i]["after_hours"] - steps[i - 1]["after_hours"]
    if hours < 24:
        return f"wait {int(hours)}h"
    d = hours / 24
    return f"wait {d:.0f} day" + ("s" if d >= 2 else "")


def _first_contact(steps: list[dict]) -> int:
    for i, s in enumerate(steps):
        if s["channel"] not in ("silent", "reconcile", "human_review",
                                "write_off"):
            return i
    return -1


def _has_mandate(wf) -> bool:
    return False        # this account holds none; see workflows.yaml
