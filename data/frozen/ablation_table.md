### Ablation table — 240 frozen scenarios, base regime

Simulated recovered value, Rs per scenario. Paired bootstrap (B = 2000, percentile).

| arm | mean net | vs B2 | 95% CI on the delta |
|---|---:|---:|---|
| O\* — oracle detection + oracle diagnosis | 473,448 | +260,398 | [+189,628, +353,096] |
| B1 — real detector + oracle diagnosis | 302,892 | +89,843 | [+44,560, +160,333] |
| B2 — real detector + L1/L2  **(the system)** | **213,049** | — | — |
| B3 — real detector + L1/L2/L3 | 213,049 | +0 | [+0, +0]  *(CI includes 0)* |
| B0 — blind retry, no detector | -1,803,750 | -2,016,799 | [-2,188,572, -1,835,471] |

### Error decomposition

| term | value | 95% CI | excludes zero |
|---|---:|---|---|
| detection regret (O* - B1) | +170,555 | [+126,240, +219,037] | yes |
| attribution regret (B1 - B3) | +89,843 | [+44,560, +160,333] | yes |
| L1/L2 gap (B1 - B2) | +89,843 | [+44,560, +160,333] | yes |
| **value added by L3 (B3 - B2)** | +0 | [+0, +0] | **no** |

### Sensitivity — the same 240 scenarios under all three regimes

| arm | pessimistic | base | optimistic |
|---|---:|---:|---:|
| O\* — oracle detection + oracle diagnosis | 339,560 | 473,448 | 602,909 |
| B1 — real detector + oracle diagnosis | 216,830 | 302,892 | 386,064 |
| B2 — real detector + L1/L2  **(the system)** | 120,149 | 213,049 | 300,314 |
| B3 — real detector + L1/L2/L3 | 120,149 | 213,049 | 300,314 |
| B0 — blind retry, no detector | -2,426,163 | -1,803,750 | -1,290,576 |

**Attribution regret across regimes** — the headline quantity:

| regime | attribution regret | 95% CI |
|---|---:|---|
| pessimistic | +96,681 | [+48,278, +169,413] |
| base | +89,843 | [+44,560, +160,333] |
| optimistic | +85,750 | [+40,999, +157,070] |

### Detection and negative-class behaviour

| metric | value | 95% CI |
|---|---:|---|
| detection recall | 53.2% | [46.6%, 59.7%] |
| incident precision | 5.4% | [4.5%, 6.4%] |
| false-ALERT rate on 48 nulls | 100.0% | [92.6%, 100.0%] |
| false-INTERVENTION rate, B2 | 91.7% | [80.4%, 96.7%] |
| false-INTERVENTION rate, B1 (oracle diagnosis) | 0.0% | — |
| attribution accuracy | 79.5% | [71.3%, 85.8%] |
| — at coverage | 79.5% x 117/117 matched = 79.5% | — |
