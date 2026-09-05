"""End-to-end pipeline. SPEC §14 Day 3 acceptance:
"A diagnosis and a bounded action are produced end to end for one scenario."

    generate -> seasonal fit -> CUSUM -> merge -> evidence -> ladder
             -> policy -> bounds -> audit

Lives outside src/ because it imports the generator, which src/attribution/ and
src/detector/ may not (§1.2).

Usage:  python -m scripts.run_pipeline [mechanism] [seed]
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

from src import audit
from src.attribution.evidence import (CellEvidence, IncidentEvidence,
                                      load_eval, load_topology, residual_shift)
from src.attribution.ladder import diagnose
from src.detector.cusum import run_cusum
from src.incident import AlertContext, dominant_source, merge_alerts
from src.policy import IncidentBudget, apply_bounds, decide, should_escalate
from src.seasonal import fit_binomial_glm
from src.simulator.generate import EPOCH, WARMUP_WINDOWS, generate_scenario

EVAL = load_eval()
TOPOLOGY = load_topology()


def detect(stream, per_source):
    """Seasonal fit on warm-up, CUSUM forward over the evaluation period.

    `per_source` supplies each alert's expected per-source counts so the
    dominant source is chosen on EXCESS over baseline, not raw volume -- see
    incident.dominant_source.
    """
    t_warm = np.arange(WARMUP_WINDOWS)
    t_eval = np.arange(WARMUP_WINDOWS, stream.n_windows)
    h = EVAL["detector"]["h"]
    contexts = []

    for ci, (issuer, method) in enumerate(stream.cells):
        n = stream.n_attempts[:, ci]
        total = stream.failures[ci].sum(axis=1)
        model = fit_binomial_glm(total[:WARMUP_WINDOWS], n[:WARMUP_WINDOWS], t_warm)
        p0 = model.predict(t_eval)
        for a in run_cusum(total[WARMUP_WINDOWS:], n[WARMUP_WINDOWS:], p0, h,
                           cell_index=ci):
            s0, s1 = a.start_window + WARMUP_WINDOWS, a.end_window + WARMUP_WINDOWS
            span = np.arange(s0, s1 + 1)
            exp = np.array([float((per_source[ci][src].predict(span)
                                   * stream.n_attempts[s0:s1 + 1, ci]).sum())
                            for src in stream.sources[ci]])
            src = dominant_source(stream.failures[ci], stream.sources[ci],
                                  s0, s1, expected=exp)
            contexts.append(AlertContext(type(a)(ci, s0, s1, a.peak),
                                         issuer, method, src))
    return contexts


def build_evidence(stream, incident, per_source_models) -> IncidentEvidence:
    """Assemble what the ladder is allowed to see for one incident."""
    src = incident.dominant_source
    methods = incident.methods
    alerting_keys = set(incident.cells)
    s0, s1 = incident.start_window, incident.end_window + 1

    cells = []
    for ci, (issuer, method) in enumerate(stream.cells):
        if method not in methods or src not in stream.sources[ci]:
            continue
        j = stream.sources[ci].index(src)
        x = int(stream.failures[ci][s0:s1, j].sum())
        n = int(stream.n_attempts[s0:s1, ci].sum())
        p0 = float(np.mean(per_source_models[ci][src].predict(np.arange(s0, s1))))
        shift, lo, hi = residual_shift(x, n, p0)
        from src.attribution.evidence import one_proportion_z
        cells.append(CellEvidence(
            issuer=issuer, method=method, n_attempts=n, failures=x,
            baseline_p=p0, shift=shift, shift_lo=lo, shift_hi=hi,
            z=one_proportion_z(x, n, p0),
            alerting=f"issuer:{issuer}|{method}" in alerting_keys,
        ))

    return IncidentEvidence(
        incident_id=incident.incident_id, dominant_source=src,
        # NOT observable from a TelemetryWindow -- see evidence.mechanism_family
        dominant_step=None,
        methods=methods, cells=cells, topology=TOPOLOGY,
    )


def fit_per_source(stream):
    t_warm = np.arange(WARMUP_WINDOWS)
    out = []
    for ci in range(len(stream.cells)):
        n = stream.n_attempts[:WARMUP_WINDOWS, ci]
        out.append({s: fit_binomial_glm(stream.failures[ci][:WARMUP_WINDOWS, j], n, t_warm)
                    for j, s in enumerate(stream.sources[ci])})
    return out


def run(scenario_id: str, kind: str, seed: int):
    stream, ledger = generate_scenario(scenario_id, kind, seed=seed)
    per_source = fit_per_source(stream)
    contexts = detect(stream, per_source)
    incidents = merge_alerts(contexts, TOPOLOGY)

    records = []
    for inc in incidents:
        ev = build_evidence(stream, inc, per_source)
        dx = diagnose(ev, EVAL)
        action = decide(dx)
        when = EPOCH + timedelta(minutes=5 * inc.start_window)
        budget = IncidentBudget()
        bounded = apply_bounds(action, when, budget)
        member = set(inc.cells)
        at_risk = int(sum(
            int(stream.amount_at_risk[inc.start_window:inc.end_window + 1, ci].sum())
            for ci, c in enumerate(stream.cells)
            if f"issuer:{c[0]}|{c[1]}" in member))
        records.append(audit.build(
            scenario_id, dx, bounded, when, inc.cells, inc.dominant_source,
            None, at_risk, escalated=should_escalate(dx, bounded)))

    return stream, ledger, incidents, records


def main() -> None:
    kind = sys.argv[1] if len(sys.argv) > 1 else "psp_degradation"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 101
    stream, ledger, incidents, records = run(f"e2e-{kind}", kind, seed)

    ep = ledger[0]
    print(f"scenario         {kind}  seed={seed}")
    print(f"TRUTH (ledger)   {ep.mechanism}  nodes={ep.affected_nodes}  "
          f"arm={ep.onset_arm}  severity={ep.severity:.2f}")
    print(f"incidents formed {len(incidents)}\n")

    for r in records:
        print(f"  {r.incident_id}  {r.level_used:<3} "
              f"cause={str(r.cause_node):<16} family={str(r.mechanism_family):<24} "
              f"conf={r.confidence:.2f}")
        print(f"      cells    {', '.join(r.cells)}")
        print(f"      source   {r.dominant_source}   at risk "
              f"Rs {r.amount_at_risk_paise/100:,.0f}")
        print(f"      action   chosen={r.action_chosen} executed={r.action_executed}"
              f"{'  bounds=' + ','.join(r.bounds_fired) if r.bounds_fired else ''}"
              f"{'  ESCALATED' if r.escalated else ''}  [{r.execution_mode}]")

    out = Path(f"data/dev/audit_{kind}_{seed}.jsonl")
    audit.write(out, records)
    print(f"\nsummary  {audit.summarise(records)}")
    print(f"audit    {out}")


if __name__ == "__main__":
    main()
