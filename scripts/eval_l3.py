"""Day 7: does L3 recover the injected root? SPEC §8.3, §14 Day 7.

Done when: L3 returns a root or a NAMED abstention reason on eligible scenarios.

The direct test of L3's stated claim. §8.3 permits L3 exactly one kind of
assertion -- temporal precedence among observed co-deviating cells -- and the
v1.2 ledger records `primary_cell`, the cell the mechanism actually started at.
So L3's root can be scored against ground truth directly, independently of any
money.

Results are split by ONSET ARM (§12.5, v1.2). A null on the simultaneous arm is
expected and correct: no precedence was injected there, so there is none to
find. A null on the PROPAGATING arm is the real finding.

Usage:  python -m scripts.eval_l3 [n_per_class]
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from src.attribution import l3
from src.attribution.evidence import load_topology
from src.incident import merge_alerts
from src.simulator.generate import EPOCH, WARMUP_WINDOWS, generate_scenario
from scripts.run_baselines import CLASSES
from scripts.run_pipeline import detect, fit_per_source

TOPOLOGY = load_topology()
PARTITION_CFG = l3.load_partition_config()

KNOWN = ["issuer_degradation", "psp_degradation", "card_auth_spike",
         "upi_network_degradation"]


def build_l3_inputs(stream, per_source, source: str, w0: int, w1: int):
    """Variables: per-ISSUER, aggregated over methods, dominant source only.

    Pre-registered in partition_selector.yaml as
    `per_issuer_aggregated_over_methods`, which resolves two gates at once: the
    issuer partition yields 8 variables (inside the 5-8 band), and an issuer
    aggregated over methods runs ~130 attempts/window instead of netbanking's
    ~23, clearing the exposure floor that per-(issuer, method) cells fail.
    """
    counts, attempts, baseline = {}, {}, {}
    span = np.arange(w0, w1)
    for ci, (issuer, method) in enumerate(stream.cells):
        if source not in stream.sources[ci]:
            continue
        j = stream.sources[ci].index(source)
        x = stream.failures[ci][w0:w1, j].astype(float)
        n = stream.n_attempts[w0:w1, ci].astype(float)
        p0 = np.asarray(per_source[ci][source].predict(span), dtype=float)
        counts[issuer] = counts.get(issuer, 0.0) + x
        attempts[issuer] = attempts.get(issuer, 0.0) + n
        # exposure-weighted baseline, so aggregating methods does not distort it
        baseline[issuer] = baseline.get(issuer, 0.0) + p0 * n
    for k in baseline:
        baseline[k] = baseline[k] / np.maximum(attempts[k], 1.0)
    return counts, attempts, baseline


def run_one(scenario_id: str, kind: str, seed: int, with_stability: bool = True):
    stream, ledger = generate_scenario(scenario_id, kind, seed=seed)
    per_source = fit_per_source(stream)
    contexts = detect(stream, per_source)
    incidents = merge_alerts(contexts, TOPOLOGY)

    truth = [e for e in ledger if e.mechanism != "none"]
    if not truth or not incidents:
        return None
    ep = truth[0]

    # the incident overlapping the true episode most, as L2 would have handled
    w0t = int((ep.start - EPOCH).total_seconds() // 300)
    w1t = int((ep.end - EPOCH).total_seconds() // 300)
    def overlap(i):
        return max(0, min(i.end_window, w1t) - max(i.start_window, w0t))
    inc = max(incidents, key=overlap)
    if overlap(inc) == 0:
        return None

    # §8.3's temporal gate wants >= 288 windows of usable history, so the
    # analysis span runs back from the incident, not just across it
    w1 = min(stream.n_windows, inc.end_window + 1)
    w0 = max(WARMUP_WINDOWS, w1 - 288)
    if w1 - w0 < 288:
        w0 = max(0, w1 - 288)

    counts, attempts, baseline = build_l3_inputs(
        stream, per_source, inc.dominant_source, w0, w1)
    if len(counts) < 2:
        return None

    res = l3.run(inc.dominant_source, None, counts, attempts, baseline,
                 cfg=PARTITION_CFG, seed=seed, with_stability=with_stability)

    true_root = (ep.primary_cell.split("|")[0].replace("issuer:", "")
                 if ep.primary_cell else None)
    # CHANCE BASELINE. 1/8 is the WRONG null. Identifying the affected SET is
    # L2's job -- L3's only marginal claim is precedence WITHIN that set. So the
    # null L3 must beat is "pick a random affected issuer", i.e. 1/k where k is
    # the number of distinct issuers the mechanism touched. For
    # issuer_degradation k = 1, so naming the right issuer is worth nothing at
    # all; scoring it against 1/8 would manufacture a result out of the
    # affected-set size.
    affected_issuers = {c.split("|")[0].replace("issuer:", "")
                        for c in ep.onset_offsets}
    k = max(len(affected_issuers), 1)
    return {
        "scenario_id": scenario_id, "kind": kind, "onset_arm": ep.onset_arm,
        "severity": ep.severity, "eligible": res.eligible,
        "root": res.root_node, "true_root": true_root,
        "correct": (res.root_node == true_root) if res.root_node else None,
        "abstain_reason": res.abstain_reason, "stability": res.stability,
        "n_variables": res.n_variables, "partition": res.partition,
        "n_affected_issuers": k, "chance_within_affected": 1.0 / k,
        "root_in_affected_set": (res.root_node in affected_issuers)
                                if res.root_node else None,
    }


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    fast = "--fast" in sys.argv
    rows = []
    for kind in KNOWN:
        for i in range(n):
            r = run_one(f"l3-{kind[:6]}-{i:03d}", kind, seed=70_000 + i,
                        with_stability=not fast)
            if r:
                rows.append(r)

    Path("data/dev").mkdir(parents=True, exist_ok=True)
    Path("data/dev/l3_eval.json").write_text(json.dumps(rows, indent=2))

    print(f"L3 evaluated on {len(rows)} scenarios with a detected incident\n")
    elig = [r for r in rows if r["eligible"]]
    got = [r for r in rows if r["root"]]
    print(f"  eligibility rate   {len(elig)}/{len(rows)} = {len(elig)/max(len(rows),1):.0%}")
    print(f"  returned a root    {len(got)}/{len(rows)} = {len(got)/max(len(rows),1):.0%}")
    if got:
        acc = sum(1 for r in got if r["correct"]) / len(got)
        print(f"  root accuracy      {acc:.0%}  (vs ledger primary_cell)")

    print("\nabstention reasons:")
    for reason, c in Counter(r["abstain_reason"] for r in rows
                             if r["abstain_reason"]).most_common():
        print(f"  {c:>4}  {reason}")

    print("\nBY ONSET ARM (§12.5 -- never pooled):")
    for arm in ("propagating", "simultaneous"):
        sub = [r for r in rows if r["onset_arm"] == arm]
        if not sub:
            continue
        g = [r for r in sub if r["root"]]
        acc = (sum(1 for r in g if r["correct"]) / len(g)) if g else float("nan")
        print(f"  {arm:<14} n={len(sub):>3}  returned root {len(g)/len(sub):>4.0%}"
              f"   root accuracy {acc:>5.0%}"
              if g else
              f"  {arm:<14} n={len(sub):>3}  returned root   0%   root accuracy   n/a")

    print("\nby mechanism:")
    for k in KNOWN:
        sub = [r for r in rows if r["kind"] == k]
        if not sub:
            continue
        g = [r for r in sub if r["root"]]
        acc = f"{sum(1 for r in g if r['correct'])/len(g):.0%}" if g else "n/a"
        ch = f"{np.mean([r['chance_within_affected'] for r in g]):.0%}" if g else "n/a"
        print(f"  {k:<26} n={len(sub):>3}  root {len(g):>3}  "
              f"accuracy {acc:>5}  chance {ch:>5}")
    print("\nwritten to data/dev/l3_eval.json")


if __name__ == "__main__":
    main()
