"""Choose the detector operating point by SIMULATED RECOVERED VALUE.

SPEC §13 step 3 ("adjust thresholds, kappa, h procedure, margins") on the dev
pool, before the freeze. This is the decision deferred from Day 2, which could
not be made then because `environment.py` did not exist and there was no
objective to optimise against -- only proxies (recall, precision) that trade
against each other with no exchange rate.

The objective is B2's net value. B2 is the deployable system: same detector,
L1/L2 diagnosis, same policy. Choosing h to maximise B1 would optimise for a
system that has an oracle, and choosing it to maximise recall would ignore what
false alarms cost.

Each scenario is generated and its seasonal models fitted ONCE, then every h is
evaluated against those same fitted baselines. That is both faster and more
correct: the h values are compared on identical data, so the comparison is
paired and RNG differences cannot leak between them.

Usage:  python -m scripts.sweep_h_by_value [n_per_class]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from src.attribution.evidence import load_eval, load_topology
from src.attribution.ladder import diagnose
from src.detector.cusum import run_cusum
from src.environment import sample_efficacies
from src.eval.baselines import (ARMS, IncidentExposure, blind_exposures,
                                oracle_optimal, run_arm, true_exposures)
from src.eval.matching import match_incidents
from src.incident import AlertContext, dominant_source, merge_alerts
from src.policy import load_policy
from src.seasonal import fit_binomial_glm
from src.simulator.generate import EPOCH, WARMUP_WINDOWS, generate_scenario
from scripts.run_baselines import CLASSES, MEAN_AMOUNT_PAISE, _exposure_amounts
from scripts.run_pipeline import build_evidence, fit_per_source

EVAL = load_eval()
TOPOLOGY = load_topology()
POLICY = load_policy()

H_GRID = [4.0, 5.5, 7.0, 9.0, 12.0, 16.0, 22.0]


def _fit_overall(stream):
    t_warm = np.arange(WARMUP_WINDOWS)
    t_eval = np.arange(WARMUP_WINDOWS, stream.n_windows)
    out = []
    for ci in range(len(stream.cells)):
        n = stream.n_attempts[:, ci]
        total = stream.failures[ci].sum(axis=1)
        m = fit_binomial_glm(total[:WARMUP_WINDOWS], n[:WARMUP_WINDOWS], t_warm)
        out.append(m.predict(t_eval))
    return out


def _detect_at(stream, per_source, p0s, h):
    contexts = []
    for ci, (issuer, method) in enumerate(stream.cells):
        n = stream.n_attempts[:, ci]
        total = stream.failures[ci].sum(axis=1)
        for a in run_cusum(total[WARMUP_WINDOWS:], n[WARMUP_WINDOWS:], p0s[ci], h,
                           cell_index=ci):
            s0, s1 = a.start_window + WARMUP_WINDOWS, a.end_window + WARMUP_WINDOWS
            span = np.arange(s0, s1 + 1)
            exp = np.array([float((per_source[ci][src].predict(span)
                                   * stream.n_attempts[s0:s1 + 1, ci]).sum())
                            for src in stream.sources[ci]])
            src = dominant_source(stream.failures[ci], stream.sources[ci],
                                  s0, s1, expected=exp)
            contexts.append(AlertContext(type(a)(ci, s0, s1, a.peak, a.crossed_window),
                                         issuer, method, src))
    return contexts


def score(stream, ledger, per_source, p0s, h, seed):
    contexts = _detect_at(stream, per_source, p0s, h)
    incidents = merge_alerts(contexts, TOPOLOGY)
    diagnoses = {i.incident_id: diagnose(build_evidence(stream, i, per_source), EVAL)
                 for i in incidents}

    truth = [e for e in ledger if e.mechanism != "none"]
    m = match_incidents(incidents, truth, window_origin=EPOCH,
                        predicted_mechanisms={i: d.mechanism_family
                                              for i, d in diagnoses.items()})
    truth_of = {p.predicted.incident_id: p.true_episode for p in m.matched_pairs}

    detected = []
    for inc in incidents:
        at_risk, touched = _exposure_amounts(stream, inc.cells,
                                             inc.start_window, inc.end_window)
        ep = truth_of.get(inc.incident_id)
        detected.append(IncidentExposure(
            inc.incident_id, at_risk, touched, inc.start_window,
            ep.mechanism if ep else "none",
            ep.affected_nodes[0] if ep and ep.affected_nodes else None))

    ar, tc = {}, {}
    for ep in truth:
        w0 = int((ep.start - EPOCH).total_seconds() // 300)
        w1 = int((ep.end - EPOCH).total_seconds() // 300)
        ar[ep.episode_id], tc[ep.episode_id] = _exposure_amounts(
            stream, list(ep.onset_offsets), w0, w1)
    oracle_exp = true_exposures(truth, ar, tc, EPOCH)

    ep_amounts = {k: (ar[k], tc[k]) for k in ar}
    total_ar = float(stream.amount_at_risk[WARMUP_WINDOWS:].sum())
    total_tc = float(stream.n_attempts[WARMUP_WINDOWS:].sum()) * MEAN_AMOUNT_PAISE
    blind = blind_exposures(
        truth, ep_amounts,
        max(0.0, total_ar - sum(a for a, _ in ep_amounts.values())),
        max(0.0, total_tc - sum(t for _, t in ep_amounts.values())),
        EPOCH, WARMUP_WINDOWS)

    eff = sample_efficacies(seed=seed)
    arms = {a: run_arm(a, oracle_exp if a == "O_STAR"
                       else blind if a == "B0" else detected,
                       diagnoses, eff, EPOCH, POLICY) for a in ARMS}
    return {
        "arms": {a: o.net_paise for a, o in arms.items()},
        "attr_correct": arms["B2"].attribution_correct,
        "attr_applicable": arms["B2"].attribution_applicable,
        "interventions": arms["B2"].n_interventions,
        "tp": len(m.matched_pairs), "fp": len(m.false_positives),
        "fn": len(m.false_negatives),
        "oracle_optimal": oracle_optimal(oracle_exp, eff),
    }


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    by_h: dict[float, list] = {h: [] for h in H_GRID}

    for kind in CLASSES:
        for i in range(n):
            seed = 60_000 + hash(kind) % 1000 + i
            stream, ledger = generate_scenario(f"h-{kind[:6]}-{i:03d}", kind, seed=seed)
            per_source = fit_per_source(stream)
            p0s = _fit_overall(stream)
            for h in H_GRID:
                by_h[h].append(score(stream, ledger, per_source, p0s, h, seed))

    print(f"scenarios per h: {n * len(CLASSES)}   (paired: same data at every h)\n")
    print(f"{'h':>5} {'B2 net Rs':>12} {'B1 net Rs':>12} {'B0 net Rs':>12} "
          f"{'recall':>7} {'prec':>6} {'attr acc':>9} {'interv':>7}")
    rows = []
    for h in H_GRID:
        r = by_h[h]
        b2 = np.mean([x["arms"]["B2"] for x in r]) / 100
        b1 = np.mean([x["arms"]["B1"] for x in r]) / 100
        b0 = np.mean([x["arms"]["B0"] for x in r]) / 100
        tp = sum(x["tp"] for x in r); fp = sum(x["fp"] for x in r)
        fn = sum(x["fn"] for x in r)
        ok = sum(x["attr_correct"] for x in r); ap = sum(x["attr_applicable"] for x in r)
        iv = np.mean([x["interventions"] for x in r])
        print(f"{h:>5.1f} {b2:>12,.0f} {b1:>12,.0f} {b0:>12,.0f} "
              f"{tp/max(tp+fn,1):>7.1%} {tp/max(tp+fp,1):>6.1%} "
              f"{ok/max(ap,1):>9.1%} {iv:>7.2f}")
        rows.append({"h": h, "b2_net": b2, "b1_net": b1, "b0_net": b0,
                     "recall": tp / max(tp + fn, 1), "precision": tp / max(tp + fp, 1),
                     "attr_acc": ok / max(ap, 1), "interventions": float(iv)})

    best = max(rows, key=lambda r: r["b2_net"])
    print(f"\nbest by B2 net value: h = {best['h']}  (Rs {best['b2_net']:,.0f})")
    Path("data/dev").mkdir(parents=True, exist_ok=True)
    Path("data/dev/h_sweep.json").write_text(json.dumps(rows, indent=2))
    print("written to data/dev/h_sweep.json")


if __name__ == "__main__":
    main()
