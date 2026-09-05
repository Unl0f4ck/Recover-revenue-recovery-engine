"""Stream generation: warm-up + evaluation period + episode ledger. SPEC §5.3.

Every scenario stream is 7 clean warm-up days followed by the evaluation
period. Seasonal parameters are fit on warm-up only and frozen before the
incident window opens (§5.3), so no incident may be injected before
`WARMUP_WINDOWS`.

The observable stream (`TelemetryWindow` counts) and the episode ledger
(`TrueEpisode`) are returned as separate objects and written to separate
files. Nothing in the observable path carries a ground-truth field (§1.2).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from ..schema import TelemetryWindow, TrueEpisode
from . import failures as F
from . import mechanisms as M
from . import volume as V

WARMUP_DAYS = 7
WARMUP_WINDOWS = WARMUP_DAYS * V.WINDOWS_PER_DAY          # 2016
EVAL_DAYS = 2
EVAL_WINDOWS = EVAL_DAYS * V.WINDOWS_PER_DAY              # 576

# Incidents are placed with a margin so an episode neither starts in the first
# window of the evaluation period nor runs past its end.
PLACEMENT_MARGIN = 48                                     # windows (4h)

EPOCH = datetime(2026, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

KNOWN = ["issuer_degradation", "psp_degradation",
         "card_auth_spike", "upi_network_degradation"]
OOD = ["ood_beneficiary_credit_delay", "ood_checkout_config_regression",
       "ood_partial_psp_timeout"]


@dataclass
class Stream:
    """Observable output. No ground truth anywhere in here."""
    scenario_id: str
    cells: list[M.Cell]
    sources: list[list[str]]            # per cell, method-valid sources
    t0: int                             # first window index (0 = warm-up start)
    n_attempts: np.ndarray              # (T, n_cells)
    failures: list[np.ndarray]          # per cell: (T, n_sources_for_cell)
    amount_at_risk: np.ndarray          # (T, n_cells) paise
    step_names: list[list[str]] = None      # per cell, ordered step vocabulary
    failures_step: list[np.ndarray] = None  # per cell: (T, n_steps_for_cell)

    @property
    def n_windows(self) -> int:
        return self.n_attempts.shape[0]

    def windows(self, cell_index: int | None = None):
        """Render as `TelemetryWindow` objects (§14 Day 1 acceptance)."""
        idxs = range(len(self.cells)) if cell_index is None else [cell_index]
        for t in range(self.n_windows):
            ts = EPOCH + timedelta(minutes=V.MINUTES_PER_WINDOW * (self.t0 + t))
            for c in idxs:
                issuer, method = self.cells[c]
                yield TelemetryWindow(
                    timestamp=ts,
                    cell_id=f"issuer:{issuer}",
                    method=method,
                    n_attempts=int(self.n_attempts[t, c]),
                    failures_by_source={s: int(self.failures[c][t, j])
                                        for j, s in enumerate(self.sources[c])},
                    failures_by_step={s: int(self.failures_step[c][t, j])
                                      for j, s in enumerate(self.step_names[c])},
                    amount_at_risk_paise=int(self.amount_at_risk[t, c]),
                )


def build_cells(topology: dict) -> list[M.Cell]:
    return [(i, m) for i in topology["issuers"] for m in topology["methods"]]


def _baseline_source_probs(issuer: str, sources: list[str], t: int) -> dict[str, float]:
    eta = {s: F.baseline_eta(issuer, s, t) for s in sources}
    _, p_src = F.outcome_probabilities(eta)
    return p_src


def _precompute(cells: list[M.Cell], sources: list[list[str]]):
    """Baseline eta and baseline source-logits per cell, per diurnal phase.

    Both depend only on (issuer, source, t mod 288), so recomputing them every
    window was ~90% of generation time. Cached by phase; the incident and
    cascade terms are still applied per window on top.
    """
    P = V.WINDOWS_PER_DAY
    eta_base, logit_base = [], []
    for ci, (issuer, _) in enumerate(cells):
        srcs = sources[ci]
        e = np.array([[F.baseline_eta(issuer, s, ph) for s in srcs]
                      for ph in range(P)])
        lb = np.zeros_like(e)
        for ph in range(P):
            p = _baseline_source_probs(issuer, srcs, ph)
            lb[ph] = [np.log(p[s] / (1.0 - p[s])) for s in srcs]
        eta_base.append(e)
        logit_base.append(lb)
    return eta_base, logit_base


def generate_scenario(scenario_id: str, kind: str, seed: int,
                      topology: dict | None = None,
                      mechanisms_cfg: dict | None = None
                      ) -> tuple[Stream, list[TrueEpisode]]:
    """Generate one scenario stream plus its ledger.

    `kind` is a mechanism name, an `ood_*` name, or "none" for a null scenario.
    """
    if topology is None or mechanisms_cfg is None:
        topology, mechanisms_cfg = M.load_configs()

    rng = np.random.default_rng(seed)
    cells = build_cells(topology)
    sources = [topology["methods"][m]["layers"] for _, m in cells]
    key_to_idx = {M.cell_key(c): i for i, c in enumerate(cells)}
    neighbours = M.build_neighbours(topology, cells)

    total_w = WARMUP_WINDOWS + EVAL_WINDOWS

    # ---- episodes. Never before WARMUP_WINDOWS (§5.3).
    episodes: list[M.Episode] = []
    if kind != "none":
        lo = WARMUP_WINDOWS + PLACEMENT_MARGIN
        hi = total_w - PLACEMENT_MARGIN - 36          # 36 = max duration windows
        start = int(rng.integers(lo, hi))
        episodes.append(M.sample_episode(rng, mechanisms_cfg, topology,
                                         kind, start, f"{scenario_id}-e0"))
        # ~15% of scenarios carry a second, overlapping incident (§3.2)
        if kind in KNOWN and rng.random() < float(mechanisms_cfg["overlap_fraction"]):
            other = str(rng.choice([k for k in KNOWN if k != kind]))
            jitter = int(rng.integers(-12, 13))
            start2 = int(np.clip(start + jitter, lo, hi))
            episodes.append(M.sample_episode(rng, mechanisms_cfg, topology,
                                             other, start2, f"{scenario_id}-e1"))

    n_att = np.zeros((total_w, len(cells)), dtype=np.int32)
    at_risk = np.zeros((total_w, len(cells)), dtype=np.int64)
    fails = [np.zeros((total_w, len(sources[i])), dtype=np.int32)
             for i in range(len(cells))]
    # v1.3 step breakdown. Ordered vocabulary per cell, flattened across the
    # cell's sources so a step shared by two sources is still one column.
    step_names = []
    for ci, (_, method) in enumerate(cells):
        seen = []
        for src in sources[ci]:
            for st in F.steps_for(method, src):
                if st not in seen:
                    seen.append(st)
        step_names.append(seen)
    fails_step = [np.zeros((total_w, len(step_names[i])), dtype=np.int32)
                  for i in range(len(cells))]

    # deviation of the PREVIOUS window, keyed (cell_key, source), for the
    # cascade term. Empty => zero, so calm periods propagate nothing.
    prev_dev: dict[tuple[str, str], float] = {}
    eta_base, logit_base = _precompute(cells, sources)
    lams = [V.CellVolume(i, m).lam for i, m in cells]
    diurnal = V.diurnal_factor(np.arange(V.WINDOWS_PER_DAY))
    keys = [M.cell_key(c) for c in cells]

    for t in range(total_w):
        ph = t % V.WINDOWS_PER_DAY
        cur_dev: dict[tuple[str, str], float] = {}

        for ci, cell in enumerate(cells):
            srcs = sources[ci]
            n = int(rng.poisson(lams[ci] * diurnal[ph]))
            n_att[t, ci] = n

            eta = {s: float(eta_base[ci][ph, j]) for j, s in enumerate(srcs)}
            for ep in episodes:
                for s in srcs:
                    eta[s] += M.direct_term(ep, cell, s, t)
                    eta[s] += M.cascade_term(ep, cell, s, t, prev_dev, neighbours)

            n_ok, by_src = F.draw_counts(rng, n, eta)
            for j, s in enumerate(srcs):
                fails[ci][t, j] = by_src[s]

            # split each source's failures across its documented steps; the
            # incident's EXCESS concentrates on its signature step
            step_idx = {st: k for k, st in enumerate(step_names[ci])}
            for j, s in enumerate(srcs):
                if not by_src[s]:
                    continue
                sig = None
                for ep in episodes:
                    if (ep.source_by_method.get(cell[1]) == s
                            and M.cell_key(cell) in ep.gammas
                            and ep.start_w <= t < ep.end_w):
                        sig = M.signature_step(ep, cell[1], s)
                        break
                expected = float(np.exp(eta_base[ci][ph, j]) /
                                 (1.0 + np.exp(eta_base[ci][ph, j]))) * n
                for st, c_ in F.split_into_steps(rng, cell[1], s, by_src[s],
                                                 expected, sig).items():
                    if c_:
                        fails_step[ci][t, step_idx[st]] += c_

            n_failed = n - n_ok
            if n_failed > 0:
                at_risk[t, ci] = int(V.draw_amounts(rng, n_failed).sum())

            # deviation from the CALM baseline, for the next window's cascade
            for j, s in enumerate(srcs):
                cur_dev[(keys[ci], s)] = (F.smoothed_logit(by_src[s], max(n, 1))
                                          - float(logit_base[ci][ph, j]))

        prev_dev = cur_dev

    stream = Stream(scenario_id=scenario_id, cells=cells, sources=sources, t0=0,
                    n_attempts=n_att, failures=fails, amount_at_risk=at_risk,
                    step_names=step_names, failures_step=fails_step)
    return stream, [_to_ledger(ep) for ep in episodes] or [_null_episode(scenario_id)]


def _to_ledger(ep: M.Episode) -> TrueEpisode:
    return TrueEpisode(
        episode_id=ep.episode_id,
        mechanism=ep.mechanism,
        start=EPOCH + timedelta(minutes=V.MINUTES_PER_WINDOW * ep.start_w),
        end=EPOCH + timedelta(minutes=V.MINUTES_PER_WINDOW * ep.end_w),
        affected_nodes=list(ep.affected_nodes),
        affected_methods=sorted({m for _, m in ep.affected_cells}),
        severity=ep.severity,
        primary_cell=M.cell_key(ep.primary_cell) if ep.primary_cell else None,
        onset_arm=ep.onset_arm,
        onset_offsets=dict(ep.offsets),
        coupling_beta=ep.beta,
    )


def _null_episode(scenario_id: str) -> TrueEpisode:
    """Null scenarios still get a ledger row so the 48 nulls are countable and
    are never silently folded into an incident denominator (§12.1).
    """
    return TrueEpisode(
        episode_id=f"{scenario_id}-none", mechanism="none",
        start=EPOCH, end=EPOCH, affected_nodes=[], affected_methods=[],
        severity=0.0, primary_cell=None, onset_arm="simultaneous",
        onset_offsets={}, coupling_beta=0.0,
    )


# ------------------------------------------------------------------ io

def save_scenario(out_dir: Path, stream: Stream, ledger: list[TrueEpisode],
                  seed: int) -> None:
    """Observables and ledger go to SEPARATE files (§1.2). `src/eval/` is the
    only consumer of `*.ledger.json`.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_dir / f"{stream.scenario_id}.stream.npz",
        n_attempts=stream.n_attempts, amount_at_risk=stream.amount_at_risk,
        cells=np.array([f"{i}|{m}" for i, m in stream.cells]),
        **{f"fail_{k}": a for k, a in enumerate(stream.failures)},
    )
    (out_dir / f"{stream.scenario_id}.ledger.json").write_text(
        json.dumps({"scenario_id": stream.scenario_id, "seed": seed,
                    "episodes": [_ledger_json(e) for e in ledger]}, indent=2),
        encoding="utf-8")


def _ledger_json(e: TrueEpisode) -> dict:
    d = asdict(e)
    d["start"] = e.start.isoformat()
    d["end"] = e.end.isoformat()
    return d
