# PREREGISTRATION

SPEC §13 step 5. Written **before** `data/frozen/` is generated, so the
tag provably predates the holdout. Nothing hashed here may move
afterwards; if any of it must, the correct remedy is a new tag and an
explicit note, never a silent edit.

- Written: 2026-08-26T20:42:22+00:00
- Branch: `master`
- Commit at time of writing: `298016ee35b5afc7c29caceb40a482b713bce55e`
- `data/frozen/` contents at time of writing: **empty**

---

## Frozen configuration

| file | sha256 |
|---|---|
| `config/environment.yaml` | `85fa9694d5466c840b16cc203a03b7aa36ee719ffdb58721e92f193746721d5f` |
| `config/policy.yaml` | `cbae609decb9ebe063a02ff74e757947a42413caf8f7ca248b30b67d31ac2b59` |
| `config/eval.yaml` | `c9833722d3b109b77bb3a9ee44ae3d2338f946ac99672a0cda3594d36c06cd8a` |
| `config/partition_selector.yaml` | `d8327316681fa4e9ed3fc3e3c2e3b6279872b5c6cb869d2c8a18632ba61963ac` |
| `config/mechanisms.yaml` | `a7843208fe6ee0485bf101275609b1dbef789525e8c0c9ad458792b924e168b9` |
| `config/topology.yaml` | `a8280bc6184eab1b31c19d3aa8f14b90e51e97ed6ec64c778c2ae47e3af93c19` |
| `config/signatures.yaml` | `9a9f1de3d58cdabdbdc1051ae01259f4b095213e78fdd3d09cd426be69053fe9` |

## Frozen evaluation-critical code

Hashed because these modules carry frozen numeric constants that the
config files do not -- volume scale, baseline source log-odds, step
weights, the CUSUM statistic and cap, and the value formula itself.
Hashing config alone would leave the generator free to drift.

| file | sha256 |
|---|---|
| `src/simulator/volume.py` | `41b32b9c73a295bc81c9bc3c91b2a6eea9345036c65d278f7f83c2266e62311c` |
| `src/simulator/failures.py` | `b1c1906c0c269658c0ba54011b132108cb5a24c9a87562dfc64235f9889f9b65` |
| `src/simulator/mechanisms.py` | `4537eeb27c8aed2b8092981f9d2ff5ff5ee7454be4736706bc91b63c4a4522eb` |
| `src/simulator/generate.py` | `85f3496ed6a85e31dfdfd8527b9786451f5e67464fd26e85d259855976fda556` |
| `src/seasonal.py` | `4842adbb194f0b3104f33f3858500252ccd161cb922bedf3573b0b27420b4cf8` |
| `src/detector/cusum.py` | `0132bb45794b269323b1af7dc9ab9ebce536fada8d9fdb3ec32da7074b7d2175` |
| `src/incident.py` | `17674a06c1a8dd6506a8deca2d95fcf036f6f9d6f102f54ffe45df9935065023` |
| `src/attribution/evidence.py` | `36b54f2ab26bf329be5e55f1f6f0417cefea55cbfbe469d49ce86a98ffaaadfa` |
| `src/attribution/l1.py` | `1e533803d6a9c049b911f2a76b154bcab60c3ecc347f26f3e120342e0244b09e` |
| `src/attribution/l2.py` | `58512f61fd0b138f2fb9c82a9075bc0b0c75472916d7d39132f8ac61e7d13096` |
| `src/attribution/ladder.py` | `b8275232d2f8644c7a43ae3b2cf66690bfdb9db6da880fc8b95e22ddf85e47cc` |
| `src/policy.py` | `4cbdeaeaf7679e7b10925929bc48a0dd3f2fbc0c249591c1fc99c66cb6aef766` |
| `src/environment.py` | `ae97dde6fb3e3fcbb727d64974b33db2367214ceae44af6e2407eb8fe792b8f3` |
| `src/eval/matching.py` | `255208573b27470dfdbd8db9e05e7b1afe81a5b51ebb7649db11d512f1b2f932` |
| `src/eval/baselines.py` | `25e0b14124868122f6bb1f269c47702902516345e846d0d9d3b827a154dca283` |
| `src/eval/metrics.py` | `b36a41e5b4ac5999adc76bcb01e1295b003391a73e1118e1bbe6bf74a0b4dee1` |
| `src/eval/bootstrap.py` | `08fd0e89d91e7267b3908b613a4fbc49ead8463a2e136fcef8822bd61484c5b0` |

---

## Pre-registered decisions (§13's checklist)

| item | value | where |
|---|---|---|
| CUSUM kappa | 1.6 | `eval.yaml` |
| CUSUM h | **5.5** | `eval.yaml` |
| h selection procedure | max B2 simulated recovered value on the dev pool, 64 scenarios evaluated paired across the h grid | `scripts/sweep_h_by_value.py`, `data/dev/h_sweep.json` |
| CUSUM statistic cap | 1.5 x h (bounded CUSUM) | `detector/cusum.py` |
| alert start | CUSUM changepoint estimate, not crossing time | `detector/cusum.py` |
| incident merge | 15 min, routing-linked, same source family, connected components | `incident.py` |
| incident close | 3 quiet windows | `eval.yaml` |
| L1 min shift | 0.55 log-odds | `eval.yaml` |
| L1 peer band | Wilson z = 1.96 | `eval.yaml` |
| L1 confidence | 0.9 (fixed by §8.1) | `eval.yaml` |
| L2 winner margin | z >= 1.5 over runner-up, else UNKNOWN | `eval.yaml` |
| L2 min candidate | z >= 2.0 | `eval.yaml` |
| L2 candidate scoring | one common pooled two-proportion test for cells and topology nodes alike | `attribution/l2.py` |
| matching threshold | 0.3, temporal IoU x topology Jaccard | `eval.yaml` |
| matcher | attribution-blind; mechanism family is a debug column only | `eval/matching.py` |
| L3 partition selector | pre-registered table, keyed on (source, step) | `partition_selector.yaml` |
| L3 variable set | per-issuer, aggregated over methods | `partition_selector.yaml` |
| L3 gates | >=30 attempts/cell/window, >=288 windows, 5-8 variables | `partition_selector.yaml` |
| PCMCI | ParCorr, tau_max = 6, alpha = 0.05, BH-FDR on the final MCI edge set | `partition_selector.yaml` |
| L3 stability | block bootstrap B = 100, root must win >= 70% | `partition_selector.yaml` |
| bootstrap | B = 2000, percentile, paired on scenarios | `eval.yaml` |
| McNemar | exact binomial on discordant pairs | `eval/bootstrap.py` |
| onset mixture | 60% propagating / 40% simultaneous | `mechanisms.yaml` |
| onset offsets | Discrete{0,1,2,3,4} windows | `mechanisms.yaml` |
| severity jitter | gamma x U(0.6, 1.4) per cell | `mechanisms.yaml` |
| step concentration | 75% of excess on the signature step | `simulator/failures.py` |
| efficacy ranges | full mechanism x action cross product | `environment.yaml` |
| value formula | at_risk x p_recover - cost_base x cost - contact penalty | `environment.py` (§10.1) |
| cost base | per-action; only SWITCH_PSP charges against touched flow | `environment.yaml` |
| sensitivity regimes | pessimistic / base / optimistic | `environment.yaml` |
| seeds | dev pool 9000, frozen 20260901, bootstrap 20260826 | `eval.yaml` |

## Frozen set composition (§12.1)

240 scenarios: 160 known-mechanism (40 each x 4), 48 null, 32 OOD.
The 48 nulls are deliberately **not** incidents and are never folded
into an incident denominator.

## Metric definitions

- **Attribution accuracy is always reported at coverage.** Accuracy
  alone is not reportable: a system abstaining on 80% posts beautiful
  conditional accuracy while being useless (§12.5).
- **L3 metrics are split by onset arm** (propagating vs simultaneous)
  and never pooled. A null on the simultaneous arm is expected and
  correct; a null on the propagating arm is the real finding (§12.5).
- **Policy regret** (`oracle_optimal - O*`) is reported alongside the
  §12.3 decomposition. O* has oracle diagnosis but still runs the fixed
  §9 table, and §10 requires the optimal action to flip between draws,
  so the table is sometimes wrong even given a perfect diagnosis.
  Without this term, policy regret inflates what looks like detection
  regret.
- All monetary figures are **simulated recovered value**, never
  production revenue (§1.2).

## Known caveat on the L3 rows

L3 is implemented on Day 7, after this tag. Its gates and PCMCI
settings above are copied from §8.3 and were not tuned -- there was
nothing to tune them against. If L3 cannot run under them, changing
them invalidates this tag and requires a second tag (`prereg-l3`)
before B3 is scored on the frozen set. Recorded here rather than
discovered later.

---

# POST-TAG AMENDMENT — 27 Aug 2026

Recorded **after** tag `prereg-v1` and after the frozen run. Appended rather
than edited into the text above, so the original pre-registration stays
auditable.

## A1. A metric was MISLABELLED. The quantity is unchanged; its name was wrong.

The row above reading *"Policy regret (`oracle_optimal - O*`)"* is **not policy
regret** and must not be reported as such.

`environment.best_action` receives `eff` — the scenario's **drawn** efficacies —
and returns the argmax over actions. Those draws are random per-scenario values
no deployable system can observe at decision time. The quantity is therefore a
**hindsight action-selection gap**: an upper bound on what any action rule could
achieve *with foreknowledge of the draws*.

Genuine policy regret would require a comparator action rule selected on dev
data before the frozen evaluation, using only decision-time information. **This
build contains no such comparator, so no policy-regret figure is quoted
anywhere.**

Corrected name: **hindsight action-selection gap = ₹175,357/scenario**. It is
reported *beside* the §12.3 decomposition as context, never inside it as an
error term.

**No computed value changed.** Only docstrings in `src/environment.py` and
`src/eval/baselines.py` were edited. Verified mechanically: after stripping
docstrings, the ASTs of both files are identical to their pre-amendment
versions. The frozen results in `data/frozen/results_base.json` are unaffected
and were not re-run. The sha256 rows above are therefore stale for those two
files by exactly one docstring each; the executable content they were
protecting is untouched.

## A2. Reporting corrections (no computation affected)

- **The ₹473,448 benchmark is not a "perfect-information ceiling."** It is the
  **oracle-detection + oracle-diagnosis value under the frozen §9 policy**. The
  policy itself can be suboptimal, so "perfect information" overstates it.
- **False-alert and false-intervention rates are reported separately.** They are
  different defects: on the 48 nulls the detector's false-alert rate is
  **100%** (8.0 incidents per scenario) while B2's false-intervention rate is
  **91.7%**. An earlier draft conflated them and wrongly implied the detector
  was not contributing noise. It is.
- **Null scenarios are not "healthy days."** One scenario is a 2-day evaluation
  window across 24 cells. The metric is reported as *44/48 null scenarios
  (91.7%) saw at least one automated intervention*.
- **B0's framing.** Under `true_mechanism = "none"` every action including
  `SAME_RAIL_RETRY` has recovery probability exactly 0.0 (`environment.yaml`).
  Ordinary background failures are therefore modelled as entirely
  non-retryable, which is a strong assumption — real transient failures are
  sometimes retryable. B0 is a deliberately naive **retry-every-failure**
  baseline and is **not** representative of an optimised production retry
  system. The frozen environment is unchanged; the framing is corrected.
- **The headline is the attribution term, not the B0 gap.** ₹2.02M is the value
  of the whole system against a deliberately crude comparator. The clean causal
  quantity for value lost to diagnosis is **₹89,843/scenario** (B1 − B3), which
  holds the detector and policy fixed. That is the number matching the
  pre-registered thesis.
