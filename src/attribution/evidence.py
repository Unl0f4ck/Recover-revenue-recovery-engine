"""Shared evidence construction for the attribution ladder. SPEC §8.

Everything the ladder is allowed to see about an incident, assembled once:
observed counts, the seasonal baseline, per-cell residual log-odds shift with
a Wilson interval, and the routing metadata of §3.1.

Leakage (§1.2): reads `topology.yaml` (known routing metadata, explicitly
permitted by §8.2), `signatures.yaml` (the public error taxonomy, Day 3 split)
and `eval.yaml`. Never the generator config, the efficacy matrix or the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

CONFIG = Path(__file__).resolve().parents[2] / "config"


def load_topology() -> dict:
    return yaml.safe_load((CONFIG / "topology.yaml").read_text(encoding="utf-8"))


def load_signatures() -> dict:
    return yaml.safe_load((CONFIG / "signatures.yaml").read_text(encoding="utf-8"))


def load_eval() -> dict:
    return yaml.safe_load((CONFIG / "eval.yaml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ stats

def wilson_interval(x: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion (§8.1, §8.2, §12.4).

    Preferred over the normal approximation because source rates are small and
    windows are short, exactly where the Wald interval misbehaves.
    """
    if n <= 0:
        return 0.0, 1.0
    p = x / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z / d * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return float(max(0.0, centre - half)), float(min(1.0, centre + half))


def _logit(p: float) -> float:
    p = float(np.clip(p, 1e-9, 1 - 1e-9))
    return float(np.log(p / (1 - p)))


def residual_shift(x: int, n: int, p0: float, z: float = 1.96
                   ) -> tuple[float, float, float]:
    """Residual log-odds shift vs the seasonal baseline, with a Wilson-derived
    interval: (shift, lo, hi).

    The interval is computed on the proportion and then mapped through logit,
    which is monotone, so the transformed endpoints remain a valid interval.
    """
    q = (x + 0.5) / (n + 1.0)               # §6 smoothing
    lo_p, hi_p = wilson_interval(x, n, z)
    base = _logit(p0)
    return _logit(q) - base, _logit(max(lo_p, 1e-9)) - base, _logit(min(hi_p, 1 - 1e-9)) - base


def one_proportion_z(x: int, n: int, p0: float) -> float:
    """Standardized deviation of an observed rate from its baseline."""
    if n <= 0:
        return 0.0
    p0 = float(np.clip(p0, 1e-9, 1 - 1e-9))
    se = np.sqrt(p0 * (1 - p0) / n)
    return float((x / n - p0) / se) if se > 0 else 0.0


def two_proportion_z(x1: int, n1: int, x2: int, n2: int) -> float:
    """Pooled two-proportion test (§8.2 clause 3): cells under a topology node
    against cells not under it.
    """
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p = (x1 + x2) / (n1 + n2)
    se = np.sqrt(p * (1 - p) * (1.0 / n1 + 1.0 / n2))
    return float((x1 / n1 - x2 / n2) / se) if se > 0 else 0.0


# ----------------------------------------------------------------- evidence

@dataclass(frozen=True)
class CellEvidence:
    issuer: str
    method: str
    n_attempts: int
    failures: int                   # of the incident's dominant source
    baseline_p: float
    shift: float
    shift_lo: float
    shift_hi: float
    z: float
    alerting: bool

    @property
    def key(self) -> str:
        return f"issuer:{self.issuer}|{self.method}"

    @property
    def deviates(self) -> bool:
        """The Wilson band excludes 'no shift' (§8.1's peer test)."""
        return self.shift_lo > 0.0


@dataclass
class IncidentEvidence:
    """Everything the ladder may see for one incident."""
    incident_id: str
    dominant_source: str
    dominant_step: str | None
    methods: list[str]
    cells: list[CellEvidence]
    topology: dict = field(default_factory=load_topology)

    @property
    def alerting(self) -> list[CellEvidence]:
        return [c for c in self.cells if c.alerting]

    @property
    def peers(self) -> list[CellEvidence]:
        """Non-alerting cells carrying the same method scope -- the comparison
        set for L1's peer test and L2's two-proportion test.
        """
        return [c for c in self.cells if not c.alerting and c.method in self.methods]

    def node_members(self, node: str) -> list[CellEvidence]:
        """Cells covered by a topology node label, e.g. 'psp:PSP_1'."""
        kind, _, name = node.partition(":")
        routing = self.topology["routing"]
        if kind == "issuer":
            return [c for c in self.cells if c.issuer == name]
        if kind == "psp":
            return [c for c in self.cells
                    if (routing.get(c.issuer) or {}).get("psp") == name]
        if kind == "network":
            return [c for c in self.cells
                    if (routing.get(c.issuer) or {}).get("network") == name
                    and c.method == "upi"]
        return []

    def candidate_nodes(self, min_cells: int) -> list[str]:
        """Topology nodes covering at least `min_cells` ALERTING cells (§8.2
        clause 2). Causal discovery is never permitted to posit a latent node
        (§8.2), so candidates come only from known routing metadata.
        """
        routing = self.topology["routing"]
        counts: dict[str, int] = {}
        for c in self.alerting:
            # AN ISSUER WITH NO KNOWN ROUTING IS NOT AN ERROR. This indexed
            # `routing[c.issuer]` directly and raised KeyError on anything the
            # topology file had not heard of -- which on a live account is
            # every issuer, because `config/topology.yaml` holds the abstract
            # ISSUER_A..H of the frozen experiment and Razorpay reports HDFC,
            # SBIN and the rest. The degradation loop was therefore guaranteed
            # to crash the first time a real book gave it enough volume to
            # reach diagnosis, and only ever escaped that because the live
            # account is too quiet to detect anything.
            #
            # Skipping is also what the spec already requires: §8.2 forbids
            # positing a latent node, so an issuer whose PSP we do not know
            # contributes no PSP candidate. It still contributes ITSELF -- we
            # observed the issuer, that is not a guess.
            route = routing.get(c.issuer) or {}
            nodes = [f"issuer:{c.issuer}"]
            if route.get("psp"):
                nodes.append(f"psp:{route['psp']}")
            if c.method == "upi" and route.get("network"):
                nodes.append(f"network:{route['network']}")
            for node in nodes:
                counts[node] = counts.get(node, 0) + 1
        return sorted(n for n, k in counts.items() if k >= min_cells)


def mechanism_family(dominant_source: str, dominant_step: str | None,
                     methods: list[str], signatures: dict | None = None
                     ) -> str | None:
    """§8.2 clause 5: family from the source/step/reason signature.

    Keys on the (source, step) PAIR, not source alone -- `issuer_degradation`
    and `card_auth_spike` share source `issuer_bank` and are separated only by
    step. See config/signatures.yaml `disambiguation`.
    """
    sig = signatures or load_signatures()
    by_source = [(name, s) for name, s in sig["families"].items()
                 if s["source"] == dominant_source]
    if not by_source:
        return None
    if len(by_source) == 1:
        return by_source[0][0]

    if dominant_step is not None:
        stepped = [name for name, s in by_source if dominant_step in s["steps"]]
        if len(stepped) == 1:
            return stepped[0]

    # KNOWN LIMITATION (Day 3, WORKLOG 27 Aug): `dominant_step` is NOT
    # observable from a TelemetryWindow. §4 gives the primary analysis unit a
    # `failures_by_source` breakdown and no step breakdown, while §8.2 clause 5
    # asks for family from the source/step/reason signature. So on card cells,
    # where issuer_degradation and card_auth_spike share source `issuer_bank`,
    # the step discriminator is unavailable and we fall back on METHOD SCOPE.
    #
    # Take the TIGHTEST family whose scope covers the observed methods:
    # card_auth_spike is cards-only, so a card-only incident is better
    # explained by it than by an issuer-wide degradation that somehow spared
    # UPI and netbanking. This is a heuristic, and it misfires when a genuine
    # issuer_degradation alerts on card alone -- both families map to
    # ALTERNATE_METHOD_LINK (§9), so the ACTION is unaffected, but attribution
    # accuracy (§12.5) is. Quantified on the dev pool before the freeze.
    scoped = [(name, s) for name, s in by_source if set(methods) <= set(s["methods"])]
    if not scoped:
        return None
    scoped.sort(key=lambda ns: (len(ns[1]["methods"]), ns[0]))
    return scoped[0][0]
