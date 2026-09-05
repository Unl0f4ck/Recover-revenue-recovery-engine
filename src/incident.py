"""Deterministic alert merge. SPEC §7.2.

Two alerts merge iff ALL THREE hold:
  1. within 15 min (3 windows) of each other,
  2. cells linked in the routing topology (shared PSP or network,
     method-valid),
  3. compatible dominant error_source family.

An incident ends when all constituent CUSUMs stay below h for 3 consecutive
windows -- which `run_cusum` already enforces when it closes an alert, so an
incident's end is the latest end among its members.

Leakage (§1.2): reads `topology.yaml` only. Routing is known operational
metadata that any payments team has (§8.2 puts the shared-PSP explanation
there deliberately); it says nothing about which node is currently degraded.
The forbidden configs -- environment.yaml, mechanisms.yaml, the ledger -- are
never touched.

TRANSITIVITY: §7.2 states the rule pairwise and does not say what happens when
A-B and B-C merge but A-C does not. We take the CONNECTED COMPONENT (transitive
closure), which is the standard alert-correlation semantics and the only choice
that makes the merge order-independent -- clique-finding would make the result
depend on which alert is considered first, which "deterministic" forbids. This
choice changes SPLIT_ERROR / MERGE_ERROR counts in §12.2 and is recorded in
WORKLOG.md as a decision, not an assumption.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from .detector.cusum import Alert

CONFIG = Path(__file__).resolve().parents[1] / "config"

MERGE_WINDOW_MINUTES = 15
MINUTES_PER_WINDOW = 5
MERGE_WINDOWS = MERGE_WINDOW_MINUTES // MINUTES_PER_WINDOW      # 3

# Sources that could share one underlying mechanism (§7.2 clause 3).
SOURCE_FAMILY = {
    "issuer_bank": "issuer",
    "gateway": "psp",
    "customer_psp": "psp",
    "network": "network",
    "beneficiary_bank": "beneficiary",
}


def load_topology() -> dict:
    return yaml.safe_load((CONFIG / "topology.yaml").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class AlertContext:
    """An alert plus the cell metadata the merge rules need."""
    alert: Alert
    issuer: str
    method: str
    dominant_source: str

    @property
    def cell_id(self) -> str:
        return f"issuer:{self.issuer}"

    @property
    def family(self) -> str:
        return SOURCE_FAMILY.get(self.dominant_source, "unknown")


@dataclass
class Incident:
    incident_id: str
    start_window: int
    end_window: int
    members: list[AlertContext] = field(default_factory=list)

    @property
    def dominant_source(self) -> str:
        """Most common dominant source among members, ties broken by name so
        the result is deterministic.
        """
        counts: dict[str, int] = {}
        for m in self.members:
            counts[m.dominant_source] = counts.get(m.dominant_source, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    @property
    def cells(self) -> list[str]:
        return sorted({f"{m.cell_id}|{m.method}" for m in self.members})

    @property
    def methods(self) -> list[str]:
        return sorted({m.method for m in self.members})


def dominant_source(failures: np.ndarray, sources: list[str],
                    start: int, end: int,
                    expected: np.ndarray | None = None) -> str:
    """The source carrying the largest EXCESS over its seasonal baseline.

    Not the largest raw count. Baseline source rates differ by an order of
    magnitude -- `issuer_bank` sits far above `gateway` in calm periods -- so
    an argmax on raw counts labels almost every incident `issuer_bank`
    regardless of what actually moved. That mislabels the source family, which
    §7.2 clause 3 then uses to decide what may merge with what, so a gateway
    incident gets split apart and its halves attributed to the wrong layer.

    `expected` is the baseline-predicted failure count per source over the same
    span. Falls back to raw counts when it is unavailable, which is only
    correct when all sources share a baseline.
    """
    seg = failures[start:max(end + 1, start + 1)].sum(axis=0).astype(float)
    if expected is None:
        return sources[int(np.argmax(seg))]
    return sources[int(np.argmax(seg - np.asarray(expected, dtype=float)))]


def _temporally_close(a: AlertContext, b: AlertContext) -> bool:
    """Clause 1. Overlapping spans count as close; otherwise the gap between
    them must be within 15 minutes.
    """
    if a.alert.start_window <= b.alert.end_window and b.alert.start_window <= a.alert.end_window:
        return True
    gap = (b.alert.start_window - a.alert.end_window
           if b.alert.start_window > a.alert.end_window
           else a.alert.start_window - b.alert.end_window)
    return gap <= MERGE_WINDOWS


def _topologically_linked(a: AlertContext, b: AlertContext, routing: dict,
                          topology: dict) -> bool:
    """Clause 2: shared PSP or shared network, method-valid for both."""
    if a.issuer == b.issuer:
        return True
    ra, rb = routing[a.issuer], routing[b.issuer]
    if ra["psp"] == rb["psp"]:
        return True
    if ra["network"] == rb["network"]:
        # networks are UPI-only (§3.1); a shared network cannot explain a link
        # between two non-UPI cells
        return a.method == "upi" and b.method == "upi"
    return False


def can_merge(a: AlertContext, b: AlertContext, routing: dict,
              topology: dict) -> bool:
    """All three clauses of §7.2."""
    return (_temporally_close(a, b)
            and _topologically_linked(a, b, routing, topology)
            and a.family == b.family)


def merge_alerts(contexts: list[AlertContext], topology: dict | None = None,
                 prefix: str = "INC") -> list[Incident]:
    """Connected-component merge. Deterministic and order-independent.

    Alerts are sorted by (start, cell, method) first so incident ids are stable
    across runs regardless of the order the detector emitted them.
    """
    if topology is None:
        topology = load_topology()
    routing = topology["routing"]

    ctxs = sorted(contexts, key=lambda c: (c.alert.start_window, c.issuer, c.method))
    n = len(ctxs)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        for j in range(i + 1, n):
            if can_merge(ctxs[i], ctxs[j], routing, topology):
                union(i, j)

    groups: dict[int, list[AlertContext]] = {}
    for i, c in enumerate(ctxs):
        groups.setdefault(find(i), []).append(c)

    incidents = []
    for k, (_, members) in enumerate(sorted(groups.items())):
        incidents.append(Incident(
            incident_id=f"{prefix}-{k:03d}",
            start_window=min(m.alert.start_window for m in members),
            end_window=max(m.alert.end_window for m in members),
            members=members,
        ))
    return incidents
