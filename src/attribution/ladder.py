"""The attribution ladder. SPEC §8.

L1 -> L2 -> (L3, Day 7). Ladder rule (§8.3): ONLY L3 abstains. L3 abstention
never erases a valid L2 diagnosis; the system still acts on L2.

L1 declining is not abstention -- it is a fall-through. L2 returning UNKNOWN is
a diagnosis of "cannot separate the explanations", which §9 maps to NO_ACTION
plus escalation. The distinction matters for §12.5, which reports abstention
rate and accuracy-at-coverage separately.
"""
from __future__ import annotations

from ..schema import Diagnosis
from . import l1, l2
from . import l3 as _l3
from .evidence import IncidentEvidence, load_eval, mechanism_family


def diagnose(ev: IncidentEvidence, cfg: dict | None = None,
             enable_l3: bool = False, l3_inputs=None,
             l3_seed: int = 0) -> Diagnosis:
    cfg = cfg or load_eval()

    r1 = l1.run(ev, cfg)
    if r1.matched:
        return Diagnosis(
            incident_id=ev.incident_id,
            cause_node=r1.cause_node,
            mechanism_family=mechanism_family(ev.dominant_source,
                                              ev.dominant_step, ev.methods),
            level_used="L1",
            confidence=r1.confidence,
            l3_eligible=False,
            l3_abstain_reason=None,
            evidence={
                "l1_reason": r1.reason,
                "alerting_cells": [c.key for c in ev.alerting],
                "dominant_source": ev.dominant_source,
                "dominant_step": ev.dominant_step,
            },
        )

    r2 = l2.run(ev, cfg)

    # ---- L3 (Day 7). WHAT L3 IS ALLOWED TO CHANGE.
    #
    # §8.3 permits L3 exactly two claims: temporal precedence among observed
    # co-deviating cells, and residual structure left after L2's topology
    # attribution. It may NOT posit an unobserved root, and it is NOT licensed
    # to reclassify the mechanism -- that comes from the source/step signature,
    # which is L2's job (§8.2 clause 5).
    #
    # So L3 refines `cause_node` and leaves `mechanism_family` alone.
    #
    # CONSEQUENCE, STATED PLAINLY: §9's policy keys on `mechanism_family` and
    # never reads `cause_node`, so B3 and B2 execute identical actions and
    # B3 - B2 is exactly zero in value BY CONSTRUCTION -- not because L3 failed.
    # This was flagged on Day 0 and is unresolved in the spec. Inventing an
    # override rule now, after seeing L3's behaviour, would be exactly the
    # result-driven design the freeze protocol exists to prevent. L3 is
    # therefore scored on its own terms: whether its root matches the ledger's
    # `primary_cell` (scripts/eval_l3.py).
    l3_res = None
    if enable_l3 and l3_inputs is not None and not r2.is_unknown:
        l3_res = _l3.run(ev.dominant_source, ev.dominant_step,
                         *l3_inputs, seed=l3_seed)

    cause = r2.cause_node
    level = "L2"
    if l3_res is not None and l3_res.succeeded:
        cause = f"issuer:{l3_res.root_node}"
        level = "L3"

    return Diagnosis(
        incident_id=ev.incident_id,
        cause_node=cause,
        mechanism_family=r2.mechanism_family,
        level_used=level,
        confidence=r2.confidence,
        l3_eligible=bool(l3_res.eligible) if l3_res else False,
        l3_abstain_reason=l3_res.abstain_reason if l3_res else None,
        evidence={
            "l3_root": l3_res.root_node if l3_res else None,
            "l3_partition": l3_res.partition if l3_res else None,
            "l3_stability": l3_res.stability if l3_res else None,
            "l1_declined": r1.reason,
            "l2_reason": r2.reason,
            "l2_margin": r2.margin,
            "candidates": [{"node": c.node, "kind": c.kind, "z": round(c.z, 3),
                            "n_cells": c.n_cells, "detail": c.detail}
                           for c in r2.candidates[:6]],
            "alerting_cells": [c.key for c in ev.alerting],
            "dominant_source": ev.dominant_source,
            "dominant_step": ev.dominant_step,
        },
    )
