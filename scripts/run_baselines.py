"""Run all baselines over the dev pool. SPEC §12.3, §14 Day 5.

Done when: B0/B1/B2 produce numbers on the dev pool.

    generate -> detect -> merge -> diagnose -> match against ledger
             -> score every arm through the SAME policy and environment

Detection runs ONCE per scenario and every arm is scored from that same pass.
Re-running detection per arm would let RNG differences leak in and quietly
break the pairing that §12.4's analysis rests on.

Usage:  python -m scripts.run_baselines [n_per_class] [regime]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from src.environment import sample_efficacies
from src.eval.baselines import (ARMS, IncidentExposure, blind_exposures,
                                oracle_optimal, run_arm, true_exposures)
from src.eval.matching import match_incidents
from src.policy import load_policy
from src.simulator.generate import EPOCH, WARMUP_WINDOWS, generate_scenario
from src.simulator.volume import AMOUNT_LOG_MU, AMOUNT_LOG_SIGMA
from scripts.run_pipeline import build_evidence, detect, fit_per_source
from src.attribution.ladder import diagnose
from src.attribution.evidence import load_eval, load_topology
from src.incident import merge_alerts

EVAL = load_eval()
TOPOLOGY = load_topology()
POLICY = load_policy()

# E[amount] for a lognormal. `touched` is the total value flowing through the
# affected cells, which the stream does not store -- it records only the FAILED
# amount (§4). Amounts are drawn iid and independently of success, so
# E[total | n] = n * E[amount] exactly. Using the expectation rather than a
# re-draw is also lower variance, which is what we want in an evaluation
# denominator.
MEAN_AMOUNT_PAISE = float(np.exp(AMOUNT_LOG_MU + AMOUNT_LOG_SIGMA ** 2 / 2))

CLASSES = ["issuer_degradation", "psp_degradation", "card_auth_spike",
           "upi_network_degradation", "none",
           "ood_beneficiary_credit_delay", "ood_checkout_config_regression",
           "ood_partial_psp_timeout"]


def _cell_index(stream) -> dict[str, int]:
    return {f"issuer:{i}|{m}": k for k, (i, m) in enumerate(stream.cells)}


def _exposure_amounts(stream, cells: list[str], w0: int, w1: int
                      ) -> tuple[float, float]:
    """(at_risk, touched) over a set of cells and a window span."""
    idx = _cell_index(stream)
    at_risk = touched = 0.0
    for c in cells:
        k = idx.get(c)
        if k is None:
            continue
        at_risk += float(stream.amount_at_risk[w0:w1 + 1, k].sum())
        touched += float(stream.n_attempts[w0:w1 + 1, k].sum()) * MEAN_AMOUNT_PAISE
    return at_risk, touched


def run_scenario(scenario_id: str, kind: str, seed: int, regime: str = "base"):
    stream, ledger = generate_scenario(scenario_id, kind, seed=seed)
    per_source = fit_per_source(stream)
    contexts = detect(stream, per_source)
    incidents = merge_alerts(contexts, TOPOLOGY)

    diagnoses = {}
    for inc in incidents:
        diagnoses[inc.incident_id] = diagnose(build_evidence(stream, inc, per_source),
                                              EVAL)

    truth = [e for e in ledger if e.mechanism != "none"]
    m = match_incidents(incidents, truth, window_origin=EPOCH,
                        predicted_mechanisms={i: d.mechanism_family
                                              for i, d in diagnoses.items()})

    # which true episode (if any) each detected incident actually is
    truth_of: dict[str, object] = {p.predicted.incident_id: p.true_episode
                                   for p in m.matched_pairs}

    detected_exposures = []
    for inc in incidents:
        at_risk, touched = _exposure_amounts(stream, inc.cells,
                                             inc.start_window, inc.end_window)
        ep = truth_of.get(inc.incident_id)
        ev_cells = build_evidence(stream, inc, per_source).alerting
        detected_exposures.append(IncidentExposure(
            incident_id=inc.incident_id,
            at_risk_paise=at_risk, touched_paise=touched,
            start_window=inc.start_window,
            persistence_windows=inc.end_window - inc.start_window + 1,
            max_effect_logodds=max((c.shift for c in ev_cells), default=0.0),
            # an incident matching no true episode IS a false alarm on a
            # healthy segment -- "none" is the correct mechanism for it, and
            # that is what makes false interventions cost money (§10)
            true_mechanism=ep.mechanism if ep else "none",
            true_cause_node=(ep.affected_nodes[0]
                             if ep and ep.affected_nodes else None),
        ))

    # oracle detection: one exposure per TRUE episode, found or not
    at_risk_by_ep, touched_by_ep = {}, {}
    for ep in truth:
        w0 = int((ep.start - EPOCH).total_seconds() // 300)
        w1 = int((ep.end - EPOCH).total_seconds() // 300)
        a, t = _exposure_amounts(stream, list(ep.onset_offsets), w0, w1)
        at_risk_by_ep[ep.episode_id], touched_by_ep[ep.episode_id] = a, t
    oracle_exposures = true_exposures(truth, at_risk_by_ep, touched_by_ep, EPOCH)

    # B0 is detector-independent (§12.3): it retries everything, always. Its
    # background exposure is all failed value in the evaluation period that no
    # episode accounts for.
    ep_amounts = {k: (at_risk_by_ep[k], touched_by_ep[k]) for k in at_risk_by_ep}
    total_at_risk = float(stream.amount_at_risk[WARMUP_WINDOWS:].sum())
    total_touched = float(stream.n_attempts[WARMUP_WINDOWS:].sum()) * MEAN_AMOUNT_PAISE
    bg_at_risk = max(0.0, total_at_risk - sum(a for a, _ in ep_amounts.values()))
    bg_touched = max(0.0, total_touched - sum(t for _, t in ep_amounts.values()))
    blind = blind_exposures(truth, ep_amounts, bg_at_risk, bg_touched,
                            EPOCH, WARMUP_WINDOWS)

    eff = sample_efficacies(seed=seed, regime=regime)
    out = {}
    for arm in ARMS:
        exposures = (oracle_exposures if arm == "O_STAR"
                     else blind if arm == "B0" else detected_exposures)
        out[arm] = run_arm(arm, exposures, diagnoses, eff, EPOCH, POLICY)

    return {
        "scenario_id": scenario_id, "kind": kind, "seed": seed,
        "n_incidents": len(incidents), "n_true": len(truth),
        "tp": len(m.matched_pairs), "fp": len(m.false_positives),
        "fn": len(m.false_negatives),
        "arms": {a: {"net": o.net_paise, "recovered": o.recovered_paise,
                     "cost": o.cost_paise + o.penalty_paise,
                     "interventions": o.n_interventions,
                     "attr_correct": o.attribution_correct,
                     "attr_applicable": o.attribution_applicable}
                 for a, o in out.items()},
        "oracle_optimal": oracle_optimal(oracle_exposures, eff),
    }


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    regime = sys.argv[2] if len(sys.argv) > 2 else "base"

    rows = []
    for kind in CLASSES:
        for i in range(n):
            rows.append(run_scenario(f"dev-{kind[:6]}-{i:03d}", kind,
                                     seed=50_000 + hash(kind) % 1000 + i,
                                     regime=regime))

    Path("data/dev").mkdir(parents=True, exist_ok=True)
    Path(f"data/dev/baselines_{regime}.json").write_text(json.dumps(rows, indent=2))

    print(f"regime={regime}   scenarios={len(rows)} ({n} per class)\n")
    print(f"{'arm':<8} {'mean net Rs':>14} {'recovered':>12} {'cost':>12} "
          f"{'interventions':>14} {'attr acc':>9}")
    for arm in ARMS:
        net = np.mean([r["arms"][arm]["net"] for r in rows]) / 100
        rec = np.mean([r["arms"][arm]["recovered"] for r in rows]) / 100
        cost = np.mean([r["arms"][arm]["cost"] for r in rows]) / 100
        iv = np.mean([r["arms"][arm]["interventions"] for r in rows])
        ok = sum(r["arms"][arm]["attr_correct"] for r in rows)
        ap = sum(r["arms"][arm]["attr_applicable"] for r in rows)
        acc = f"{ok/ap:.1%}" if ap else "n/a"
        print(f"{arm:<8} {net:>14,.0f} {rec:>12,.0f} {cost:>12,.0f} "
              f"{iv:>14.2f} {acc:>9}")

    opt = np.mean([r["oracle_optimal"] for r in rows]) / 100
    o = np.mean([r["arms"]["O_STAR"]["net"] for r in rows]) / 100
    b1 = np.mean([r["arms"]["B1"]["net"] for r in rows]) / 100
    b2 = np.mean([r["arms"]["B2"]["net"] for r in rows]) / 100
    print(f"\nregret decomposition (mean Rs per scenario)")
    print(f"  policy regret     (opt - O*)  {opt - o:>12,.0f}")
    print(f"  detection regret  (O* - B1)   {o - b1:>12,.0f}")
    print(f"  attribution gap   (B1 - B2)   {b1 - b2:>12,.0f}")
    tp = sum(r["tp"] for r in rows); fp = sum(r["fp"] for r in rows)
    fn = sum(r["fn"] for r in rows)
    print(f"\ndetection  TP={tp}  FP={fp}  FN={fn}   "
          f"recall={tp/max(tp+fn,1):.1%}  precision={tp/max(tp+fp,1):.1%}")
    print(f"written to data/dev/baselines_{regime}.json")


if __name__ == "__main__":
    main()
