"""The ablation table with CIs. SPEC §14 Day 8 acceptance.

Reads the frozen results already produced (never re-runs them) and emits the
table in markdown, ready for RESULTS.md and the video.

Usage:  python -m scripts.ablation_table
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from src.eval.bootstrap import paired_bootstrap_delta, wilson_interval

FROZEN = Path("data/frozen")
REGIMES = ["pessimistic", "base", "optimistic"]
ARMS = ["O_STAR", "B1", "B2", "B3", "B0"]
LABEL = {
    "O_STAR": "O\\* — oracle detection + oracle diagnosis",
    "B1": "B1 — real detector + oracle diagnosis",
    "B2": "B2 — real detector + L1/L2  **(the system)**",
    "B3": "B3 — real detector + L1/L2/L3",
    "B0": "B0 — blind retry, no detector",
}


def load(regime: str):
    p = FROZEN / f"results_{regime}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main() -> None:
    base = load("base")
    if base is None:
        raise SystemExit("run scripts.run_frozen first")

    arms = {a: np.array([r["arms"][a]["net"] for r in base]) / 100 for a in ARMS}
    n = len(base)
    out = []

    out.append("### Ablation table — 240 frozen scenarios, base regime\n")
    out.append("Simulated recovered value, Rs per scenario. Paired bootstrap "
               "(B = 2000, percentile).\n")
    out.append("| arm | mean net | vs B2 | 95% CI on the delta |")
    out.append("|---|---:|---:|---|")
    for a in ARMS:
        if a == "B2":
            out.append(f"| {LABEL[a]} | **{arms[a].mean():,.0f}** | — | — |")
            continue
        d = paired_bootstrap_delta(arms[a], arms["B2"])
        star = "" if d.excludes_zero else "  *(CI includes 0)*"
        out.append(f"| {LABEL[a]} | {arms[a].mean():,.0f} | {d.point:+,.0f} | "
                   f"[{d.ci_lo:+,.0f}, {d.ci_hi:+,.0f}]{star} |")

    out.append("\n### Error decomposition\n")
    out.append("| term | value | 95% CI | excludes zero |")
    out.append("|---|---:|---|---|")
    for name, a, b in [("detection regret (O* - B1)", "O_STAR", "B1"),
                       ("attribution regret (B1 - B3)", "B1", "B3"),
                       ("L1/L2 gap (B1 - B2)", "B1", "B2"),
                       ("**value added by L3 (B3 - B2)**", "B3", "B2")]:
        d = paired_bootstrap_delta(arms[a], arms[b])
        out.append(f"| {name} | {d.point:+,.0f} | "
                   f"[{d.ci_lo:+,.0f}, {d.ci_hi:+,.0f}] | "
                   f"{'yes' if d.excludes_zero else '**no**'} |")

    out.append("\n### Sensitivity — the same 240 scenarios under all three regimes\n")
    out.append("| arm | pessimistic | base | optimistic |")
    out.append("|---|---:|---:|---:|")
    loaded = {r: load(r) for r in REGIMES}
    for a in ARMS:
        cells = []
        for r in REGIMES:
            rows = loaded[r]
            cells.append(f"{np.mean([x['arms'][a]['net'] for x in rows])/100:,.0f}"
                         if rows else "—")
        out.append(f"| {LABEL[a]} | " + " | ".join(cells) + " |")

    out.append("\n**Attribution regret across regimes** — the headline quantity:\n")
    out.append("| regime | attribution regret | 95% CI |")
    out.append("|---|---:|---|")
    for r in REGIMES:
        rows = loaded[r]
        if not rows:
            continue
        b1 = np.array([x["arms"]["B1"]["net"] for x in rows]) / 100
        b3 = np.array([x["arms"]["B3"]["net"] for x in rows]) / 100
        d = paired_bootstrap_delta(b1, b3)
        out.append(f"| {r} | {d.point:+,.0f} | [{d.ci_lo:+,.0f}, {d.ci_hi:+,.0f}] |")

    tp = sum(r["tp"] for r in base); fp = sum(r["fp"] for r in base)
    fn = sum(r["fn"] for r in base)
    rl, rh = wilson_interval(tp, tp + fn)
    pl, ph = wilson_interval(tp, tp + fp)
    nulls = [r for r in base if r["kind"] == "none"]
    fi = sum(1 for r in nulls if r["arms"]["B2"]["interventions"] > 0)
    fa = sum(1 for r in nulls if r["n_incidents"] > 0)
    fil, fih = wilson_interval(fi, len(nulls))
    fal, fah = wilson_interval(fa, len(nulls))

    out.append("\n### Detection and negative-class behaviour\n")
    out.append("| metric | value | 95% CI |")
    out.append("|---|---:|---|")
    out.append(f"| detection recall | {tp/max(tp+fn,1):.1%} | [{rl:.1%}, {rh:.1%}] |")
    out.append(f"| incident precision | {tp/max(tp+fp,1):.1%} | [{pl:.1%}, {ph:.1%}] |")
    out.append(f"| false-ALERT rate on {len(nulls)} nulls | {fa/len(nulls):.1%} | "
               f"[{fal:.1%}, {fah:.1%}] |")
    out.append(f"| false-INTERVENTION rate, B2 | {fi/len(nulls):.1%} | "
               f"[{fil:.1%}, {fih:.1%}] |")
    b1fi = sum(1 for r in nulls if r["arms"]["B1"]["interventions"] > 0)
    out.append(f"| false-INTERVENTION rate, B1 (oracle diagnosis) | "
               f"{b1fi/len(nulls):.1%} | — |")

    ok = sum(r["arms"]["B2"]["attr_correct"] for r in base)
    ap = sum(r["arms"]["B2"]["attr_applicable"] for r in base)
    al, ah = wilson_interval(ok, ap)
    matched_total = tp
    out.append(f"| attribution accuracy | {ok/max(ap,1):.1%} | [{al:.1%}, {ah:.1%}] |")
    out.append(f"| — at coverage | {ok/max(ap,1):.1%} x {ap}/{matched_total} matched "
               f"= {ok/max(matched_total,1):.1%} | — |")

    text = "\n".join(out) + "\n"
    Path("data/frozen/ablation_table.md").write_text(text, encoding="utf-8")
    import sys; sys.stdout.buffer.write(text.encode("utf-8"))
    print("written to data/frozen/ablation_table.md")


if __name__ == "__main__":
    main()
