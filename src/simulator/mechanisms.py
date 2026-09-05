"""Mechanism injection. SPEC §3.2 and §5.2 (v1.2 onset/propagation).

Four known mechanisms come from config/mechanisms.yaml. The OOD mechanisms are
defined HERE and only here: they are generator-only and never appear in
mechanisms.yaml as recognizable classes (§3.2). The attributor is never designed
for them; they exist to test abstention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

CONFIG = Path(__file__).resolve().parents[2] / "config"

Cell = tuple[str, str]          # (issuer, method)


def cell_key(cell: Cell) -> str:
    """Ledger/offset key. Cell ids are partition-qualified per §4."""
    return f"issuer:{cell[0]}|{cell[1]}"


def load_configs() -> tuple[dict, dict]:
    """Generator-side config. Reads BOTH halves of the Day 3 split: the hidden
    dynamics (mechanisms.yaml) and the public signature table
    (signatures.yaml). Reading signatures here rather than duplicating source
    and step means the two halves cannot drift apart.
    """
    topology = yaml.safe_load((CONFIG / "topology.yaml").read_text(encoding="utf-8"))
    mechanisms = yaml.safe_load((CONFIG / "mechanisms.yaml").read_text(encoding="utf-8"))
    signatures = yaml.safe_load((CONFIG / "signatures.yaml").read_text(encoding="utf-8"))
    for name, mech in mechanisms["mechanisms"].items():
        sig = signatures["families"][name]
        mech["dominant_source"] = sig["source"]
        mech["method_scope"] = list(sig["methods"])
        mech["signature"] = sig
    return topology, mechanisms


@dataclass
class Episode:
    """Internal generator representation. Projected to `TrueEpisode` for the
    ledger by generate.py; the observable stream never sees this object.
    """
    episode_id: str
    mechanism: str                       # incl. "ood_*"
    start_w: int
    end_w: int
    ramp: str                            # "step" | "linear"
    severity: float                      # gamma_m, log-odds
    affected_cells: list[Cell]
    affected_nodes: list[str]
    source_by_method: dict[str, str]
    primary_cell: Cell | None
    onset_arm: str                       # "propagating" | "simultaneous"
    signature_steps: list[str] = field(default_factory=list)
    offsets: dict[str, int] = field(default_factory=dict)   # cell_key -> delta
    gammas: dict[str, float] = field(default_factory=dict)  # cell_key -> gamma_{m,p}
    beta: float = 0.0

    def active_span(self) -> tuple[int, int]:
        return self.start_w, self.end_w


# ---------------------------------------------------------------- ramps

def ramp_value(kind: str, since: int, span: int) -> float:
    """Fraction of full severity at `since` windows past this cell's onset.

    step   -- full severity immediately (issuer outage, auth spike)
    linear -- degrades progressively across the span (gateway saturation,
              network congestion building)
    """
    if since < 0 or span <= 0:
        return 0.0
    if kind == "step":
        return 1.0
    frac = (since + 1) / float(span)
    return float(np.clip(frac, 0.0, 1.0))


# ------------------------------------------------- affected-set resolution

def _cells_for(topology: dict, issuers: list[str], methods: list[str]) -> list[Cell]:
    valid = topology["methods"]
    return [(i, m) for i in issuers for m in methods if m in valid]


def resolve_affected(topology: dict, affected_set: str, method_scope: list[str],
                     rng: np.random.Generator) -> tuple[list[Cell], list[str]]:
    """Returns (affected cells, topology node labels for the ledger)."""
    routing = topology["routing"]
    issuers = topology["issuers"]

    if affected_set == "one_issuer":
        iss = str(rng.choice(issuers))
        return _cells_for(topology, [iss], method_scope), [f"issuer:{iss}"]

    if affected_set == "one_issuer_cards":
        iss = str(rng.choice(issuers))
        return _cells_for(topology, [iss], ["card"]), [f"issuer:{iss}"]

    if affected_set == "all_issuers_under_one_psp":
        psp = str(rng.choice(topology["psps"]))
        under = [i for i in issuers if routing[i]["psp"] == psp]
        return _cells_for(topology, under, method_scope), [f"psp:{psp}"]

    if affected_set == "all_upi_cells_on_one_network":
        net = str(rng.choice(topology["networks"]))
        under = [i for i in issuers if routing[i]["network"] == net]
        return _cells_for(topology, under, ["upi"]), [f"network:{net}"]

    raise ValueError(f"unknown affected_set: {affected_set}")


# ------------------------------------------------------------ OOD set

def ood_specs(topology: dict) -> dict[str, dict]:
    """Generator-only mechanisms. NEVER in mechanisms.yaml (§3.2).

    Each is built to break a different assumption the attributor relies on, so
    abstention is tested rather than merely asserted.
    """
    issuers = topology["issuers"]
    return {
        # Merchant-side beneficiary bank stalls crediting. Hits every UPI cell
        # at once with a source no mechanism claims. No topology node explains
        # it, because the affected set is ALL of them.
        "ood_beneficiary_credit_delay": {
            "source_by_method": {"upi": "beneficiary_bank"},
            "cells": [(i, "upi") for i in issuers],
            "nodes": ["global:beneficiary"],
            "ramp": "linear",
            "beta": 0.10,
        },
        # A checkout config regression that inflates TWO source families at
        # once. The partition selector's "ambiguous / two signatures => no L3
        # run" rule should fire on this.
        "ood_checkout_config_regression": {
            "source_by_method": {"card": "gateway", "upi": "gateway",
                                 "netbanking": "issuer_bank"},
            "cells": [(i, m) for i in issuers for m in ("card", "upi", "netbanking")],
            "nodes": ["global:checkout"],
            "ramp": "step",
            "beta": 0.0,
        },
        # Gateway-signature failures on a set of issuers that does NOT match
        # any PSP. The signature says "look at PSPs"; no PSP explains the set.
        # The sharpest abstention test of the three.
        "ood_partial_psp_timeout": {
            "source_by_method": {"card": "gateway", "upi": "gateway"},
            "cells": None,                 # sampled: 3 issuers across PSPs
            "nodes": ["unaligned:partial"],
            "ramp": "linear",
            "beta": 0.25,
        },
    }


def _sample_ood_cells(topology: dict, name: str, spec: dict,
                      rng: np.random.Generator) -> tuple[list[Cell], list[str]]:
    if spec["cells"] is not None:
        return list(spec["cells"]), list(spec["nodes"])

    # ood_partial_psp_timeout: pick 3 issuers spanning >=2 different PSPs so no
    # single topology node covers the set.
    routing = topology["routing"]
    issuers = list(topology["issuers"])
    for _ in range(64):
        pick = list(rng.choice(issuers, size=3, replace=False))
        if len({routing[i]["psp"] for i in pick}) >= 2:
            break
    cells = [(i, m) for i in pick for m in ("card", "upi")]
    return cells, [f"unaligned:{'+'.join(sorted(pick))}"]


# --------------------------------------------------------- episode sampling

def sample_episode(rng: np.random.Generator, mechanisms_cfg: dict, topology: dict,
                   mechanism: str, start_w: int, episode_id: str) -> Episode:
    """Draw one episode, including the v1.2 onset arm, per-cell offsets and
    per-cell severity jitter.
    """
    onset = mechanisms_cfg["onset"]
    sev_lo, sev_hi = mechanisms_cfg["severity_log_odds"]
    dur_lo, dur_hi = mechanisms_cfg["duration_minutes"]

    duration_min = int(rng.integers(dur_lo, dur_hi + 1))
    span = max(1, duration_min // 5)                 # windows
    severity = float(rng.uniform(sev_lo, sev_hi))

    if mechanism.startswith("ood_"):
        spec = ood_specs(topology)[mechanism]
        cells, nodes = _sample_ood_cells(topology, mechanism, spec, rng)
        source_by_method = dict(spec["source_by_method"])
        cells = [c for c in cells if c[1] in source_by_method]
        ramp, beta = spec["ramp"], float(spec["beta"])
        sig_steps = list(spec.get("steps", []))
    else:
        mech = mechanisms_cfg["mechanisms"][mechanism]
        cells, nodes = resolve_affected(topology, mech["affected_set"],
                                        mech["method_scope"], rng)
        source_by_method = {m: mech["dominant_source"] for m in mech["method_scope"]}
        ramp, beta = mech["ramp"], float(mech["beta"])
        sig_steps = list(mech["signature"]["steps"])

    # --- v1.2 §5.2: onset arm, primary cell, offsets, severity jitter
    arm = ("propagating"
           if rng.random() < float(onset["mixture"]["propagating"])
           else "simultaneous")

    primary = cells[int(rng.integers(len(cells)))] if cells else None
    offsets: dict[str, int] = {}
    gammas: dict[str, float] = {}
    jit_lo, jit_hi = onset["severity_jitter"]

    for c in cells:
        k = cell_key(c)
        if arm == "simultaneous" or c == primary:
            offsets[k] = 0
        else:
            offsets[k] = int(rng.choice(onset["offset_windows"]))
        gammas[k] = severity * float(rng.uniform(jit_lo, jit_hi))

    return Episode(
        episode_id=episode_id,
        mechanism=mechanism,
        start_w=start_w,
        end_w=start_w + span,
        ramp=ramp,
        severity=severity,
        affected_cells=cells,
        affected_nodes=nodes,
        source_by_method=source_by_method,
        primary_cell=primary,
        onset_arm=arm,
        signature_steps=sig_steps,
        offsets=offsets,
        gammas=gammas,
        beta=0.0 if arm == "simultaneous" else beta,
    )


# ------------------------------------------------------ eta contributions

def signature_step(ep: Episode, method: str, source: str) -> str | None:
    """Which documented step this mechanism concentrates on, for this method.

    A family's `steps` list in signatures.yaml spans every method it covers
    (issuer_degradation fires at payment_authorization on card and at
    payment_debit_response on UPI). Pick the one valid for this method/source.
    """
    from .failures import STEP_WEIGHTS
    valid = STEP_WEIGHTS.get(method, {}).get(source, {})
    for step in ep.signature_steps:
        if step in valid:
            return step
    return None


def direct_term(ep: Episode, cell: Cell, source: str, t: int) -> float:
    """gamma_{m,p} * ramp_m(t - Delta_{m,p}), gated on cell membership and the
    mechanism's dominant source for that method.
    """
    if ep.source_by_method.get(cell[1]) != source:
        return 0.0
    k = cell_key(cell)
    if k not in ep.gammas:
        return 0.0
    onset_w = ep.start_w + ep.offsets.get(k, 0)
    if not (onset_w <= t < ep.end_w):
        return 0.0
    span = max(1, ep.end_w - onset_w)
    return ep.gammas[k] * ramp_value(ep.ramp, t - onset_w, span)


def cascade_term(ep: Episode, cell: Cell, source: str, t: int,
                 prev_dev: dict[tuple[str, str], float],
                 neighbours: dict[str, list[tuple[Cell, float]]]) -> float:
    """beta_m * sum_q w_pq * d_{q,s}(t-1), over topologically linked cells.

    Carries the DEVIATION d, not the raw lagged logit: on the raw form every
    cell would sit permanently above its own seasonal baseline during calm
    periods, §6's fit would absorb the offset and the detector's null would
    silently shift. On the deviation the term vanishes whenever linked cells
    are at baseline. (SPEC §5.2, corrected Day 1.)
    """
    if ep.beta == 0.0 or ep.source_by_method.get(cell[1]) != source:
        return 0.0
    k = cell_key(cell)
    if k not in ep.gammas or not (ep.start_w <= t < ep.end_w):
        return 0.0
    total = 0.0
    for q, w in neighbours.get(k, ()):
        if cell_key(q) in ep.gammas:                  # propagate within N_m only
            total += w * prev_dev.get((cell_key(q), source), 0.0)
    return ep.beta * total


def build_neighbours(topology: dict, cells: list[Cell]) -> dict[str, list[tuple[Cell, float]]]:
    """Routing-linked neighbours with normalized weights (§3.1 cascade_weights).

    Two cells are linked when they share a PSP or a network and the method is
    valid for both. Weights are normalized per cell so beta_m means the same
    thing regardless of how many neighbours a cell happens to have.
    """
    routing = topology["routing"]
    cw = topology["cascade_weights"]
    out: dict[str, list[tuple[Cell, float]]] = {}
    for p in cells:
        raw: list[tuple[Cell, float]] = []
        for q in cells:
            if q == p or q[1] != p[1]:
                continue
            w = 0.0
            if routing[p[0]]["psp"] == routing[q[0]]["psp"]:
                w = max(w, float(cw["shared_psp"]))
            if routing[p[0]]["network"] == routing[q[0]]["network"]:
                w = max(w, float(cw["shared_network"]))
            if w > 0.0:
                raw.append((q, w))
        total = sum(w for _, w in raw)
        out[cell_key(p)] = [(q, w / total) for q, w in raw] if total else []
    return out
