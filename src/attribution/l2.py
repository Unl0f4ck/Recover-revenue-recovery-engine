"""L2 — statistical attribution plus routing topology. SPEC §8.2.

  1. Per-cell residual shift with Wilson CI.
  2. Candidates: each alerting cell individually, plus each topology node
     covering >= 2 alerting cells.
  3. For each topology candidate: pooled two-proportion test, cells under the
     node vs cells not under it, conditioned on the dominant error source.
  4. Winner must beat runner-up by a pre-registered margin, else UNKNOWN.
  5. mechanism_family from the source/step/reason signature.

TOPOLOGY MAPPING LIVES HERE, NOT IN L3 (§8.2). The shared-PSP explanation comes
from known routing metadata. Causal discovery is never permitted to posit a
latent infrastructure node -- that would violate causal sufficiency and produce
confident spurious edges.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .evidence import (IncidentEvidence, load_eval, mechanism_family,
                       two_proportion_z)


@dataclass(frozen=True)
class Candidate:
    node: str
    kind: str                   # "cell" | "topology"
    z: float
    n_cells: int
    detail: str


@dataclass(frozen=True)
class L2Result:
    cause_node: str | None
    mechanism_family: str | None
    confidence: float
    candidates: list[Candidate]
    margin: float
    reason: str

    @property
    def is_unknown(self) -> bool:
        return self.cause_node is None


def _score_group(ev: IncidentEvidence, node: str, members: list, kind: str
                 ) -> Candidate:
    """Pooled two-proportion test (§8.2 clause 3), conditioned on the dominant
    source: cells under this candidate against cells not under it.

    EVERY candidate is scored by this same test, cells included. Scoring cells
    with a one-proportion test against their own baseline and topology nodes
    with a two-proportion test against their peers puts the two on different
    scales, and the cell test always wins: during a PSP-wide degradation every
    member cell is enormously significant against its OWN baseline, so the
    parts outrank the whole they belong to and L2 splits its evidence across
    them. Under the common test a single cell of a PSP-wide incident scores
    poorly -- because its comparison group contains the co-degraded siblings --
    while the PSP node scores well, since its outside group is genuinely clean.
    That is the discrimination §8.2 is asking for.
    """
    member_keys = {m.key for m in members}
    outside = [c for c in ev.cells
               if c.key not in member_keys and c.method in ev.methods]

    x1 = sum(m.failures for m in members)
    n1 = sum(m.n_attempts for m in members)
    x2 = sum(o.failures for o in outside)
    n2 = sum(o.n_attempts for o in outside)

    z = two_proportion_z(x1, n1, x2, n2)
    return Candidate(node=node, kind=kind, z=z, n_cells=len(members),
                     detail=f"{len(members)} in / {len(outside)} out")


def run(ev: IncidentEvidence, cfg: dict | None = None) -> L2Result:
    conf = (cfg or load_eval())["attribution"]["l2"]

    scored: list[Candidate] = []

    # each alerting cell individually (§8.2 clause 2)
    for c in ev.alerting:
        scored.append(_score_group(ev, f"issuer:{c.issuer}|{c.method}", [c], "cell"))

    # plus each topology node covering >= min_cells alerting cells
    for node in ev.candidate_nodes(conf["min_cells_for_topology_candidate"]):
        scored.append(_score_group(ev, node, ev.node_members(node), "topology"))

    # One score per node label, best kept. Without this an issuer appears once
    # per alerting method AND once as a topology node, so a single explanation
    # occupies both the winner and runner-up slots and the margin test
    # compares a hypothesis against itself.
    best: dict[str, Candidate] = {}
    for c in scored:
        if c.node not in best or c.z > best[c.node].z:
            best[c.node] = c

    ranked = sorted(best.values(), key=lambda c: (-c.z, c.node))
    viable = [c for c in ranked if c.z >= conf["min_candidate_z"]]

    if not viable:
        return L2Result(None, None, 0.0, ranked, 0.0,
                        f"no candidate reached z >= {conf['min_candidate_z']}")

    winner = viable[0]
    runner = viable[1] if len(viable) > 1 else None
    margin = winner.z - runner.z if runner else float("inf")

    if runner is not None and margin < conf["winner_margin_z"]:
        # §8.2 clause 4. Two explanations the data cannot separate is exactly
        # the case the spec wants escalated, not guessed.
        return L2Result(None, None, 0.0, ranked, margin,
                        f"margin {margin:.2f} below pre-registered "
                        f"{conf['winner_margin_z']} ({winner.node} vs {runner.node})")

    family = mechanism_family(ev.dominant_source, ev.dominant_step, ev.methods)

    # Confidence rises with separation but never reaches L1's 0.9: L2 is a
    # statistical call, not a deterministic one.
    spread = margin if np.isfinite(margin) else 6.0
    confidence = float(np.clip(
        conf["confidence_floor"] + 0.08 * spread,
        conf["confidence_floor"], conf["confidence_ceiling"]))

    return L2Result(winner.node, family, confidence, ranked, margin,
                    f"{winner.kind} candidate {winner.node} "
                    f"z={winner.z:.2f}, margin {margin:.2f}")
