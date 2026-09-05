"""Generate the frozen evaluation set. SPEC §12.1, §13 step 6.

240 scenarios: 160 known-mechanism (40 each x 4), 48 null, 32 OOD.

MUST run AFTER the pre-registration tag. §13's ordering is not advisory --
generating the holdout first and freezing configs afterwards is tuning on test,
whatever the intent.

The 48 nulls are deliberately NOT incidents (§12.1) and must never be folded
into an incident denominator, so they are labelled as their own class here.

WHAT IS COMMITTED. Streams are regenerated from seeds rather than stored: 240
scenarios x 24 cells x 2592 windows is a large binary payload that git handles
badly, and a seed plus a pinned generator is a stronger reproducibility claim
than a blob nobody can diff. `manifest.json` records every seed AND a sha256 of
each generated stream, so any drift in the generator shows up immediately as a
hash mismatch -- the generator modules are themselves hashed in
PREREGISTRATION.md.

Usage:  python -m scripts.generate_frozen [--verify]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from src.attribution.evidence import load_eval
from src.simulator.generate import generate_scenario

OUT = Path("data/frozen")
MANIFEST = OUT / "manifest.json"

COMPOSITION = [
    ("issuer_degradation", 40),
    ("psp_degradation", 40),
    ("card_auth_spike", 40),
    ("upi_network_degradation", 40),
    ("none", 48),
    ("ood_beneficiary_credit_delay", 11),
    ("ood_checkout_config_regression", 11),
    ("ood_partial_psp_timeout", 10),
]


def stream_digest(stream) -> str:
    """Content hash of the observable stream. Any change to the generator
    changes this, which is the point.
    """
    h = hashlib.sha256()
    h.update(stream.n_attempts.tobytes())
    h.update(stream.amount_at_risk.tobytes())
    for a in stream.failures:
        h.update(a.tobytes())
    for a in stream.failures_step:
        h.update(a.tobytes())
    return h.hexdigest()


def build(verify: bool = False) -> None:
    base_seed = int(load_eval()["seeds"]["frozen"])
    OUT.mkdir(parents=True, exist_ok=True)

    total = sum(n for _, n in COMPOSITION)
    assert total == 240, f"composition must be 240 scenarios, got {total}"

    prior = {}
    if verify:
        prior = {r["scenario_id"]: r for r in
                 json.loads(MANIFEST.read_text(encoding="utf-8"))["scenarios"]}

    rows, k, mismatches = [], 0, []
    for kind, count in COMPOSITION:
        for i in range(count):
            sid = f"frozen-{kind}-{i:03d}"
            seed = base_seed + k
            k += 1
            stream, ledger = generate_scenario(sid, kind, seed=seed)
            digest = stream_digest(stream)

            if verify:
                want = prior.get(sid, {}).get("stream_sha256")
                if want and want != digest:
                    mismatches.append(sid)
                continue

            rows.append({
                "scenario_id": sid, "kind": kind, "seed": seed,
                "stream_sha256": digest,
                "n_windows": int(stream.n_windows),
                "n_cells": len(stream.cells),
                "episodes": [{"episode_id": e.episode_id, "mechanism": e.mechanism,
                              "onset_arm": e.onset_arm,
                              "severity": round(e.severity, 6),
                              "primary_cell": e.primary_cell,
                              "affected_nodes": e.affected_nodes,
                              "n_affected_cells": len(e.onset_offsets)}
                             for e in ledger],
            })

    if verify:
        if mismatches:
            print(f"DRIFT: {len(mismatches)} scenarios no longer reproduce")
            for s in mismatches[:5]:
                print("  ", s)
            sys.exit(1)
        print(f"verified: all {total} scenarios reproduce byte-identically")
        return

    counts: dict[str, int] = {}
    arms: dict[str, int] = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        for e in r["episodes"]:
            if e["mechanism"] != "none":
                arms[e["onset_arm"]] = arms.get(e["onset_arm"], 0) + 1

    MANIFEST.write_text(json.dumps({
        "spec": "v1.3", "base_seed": base_seed, "n_scenarios": len(rows),
        "composition": dict(COMPOSITION), "scenarios": rows,
    }, indent=2), encoding="utf-8")

    known = sum(counts[k] for k, _ in COMPOSITION
                if not k.startswith("ood_") and k != "none")
    ood = sum(counts[k] for k, _ in COMPOSITION if k.startswith("ood_"))
    print(f"generated {len(rows)} scenarios")
    print(f"  known-mechanism {known}   null {counts['none']}   OOD {ood}")
    n_arm = sum(arms.values())
    for a, c in sorted(arms.items()):
        print(f"  onset arm {a:<14} {c:>4}  ({c/max(n_arm,1):.0%})")
    print(f"manifest: {MANIFEST}")


if __name__ == "__main__":
    build(verify="--verify" in sys.argv)
