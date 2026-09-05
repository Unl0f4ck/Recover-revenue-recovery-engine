# Causal Payment Recovery Engine — Implementation Spec v1.2 (FROZEN)

**v1.2 amendment (26 Aug 2026, Day 1 — pre-freeze).** Three changes, all upstream of `data/frozen/` and therefore permitted under §13: §5.2 mechanism onset/propagation, §8.3 variance standardization of `V_p(t)`, §12.5 reporting split by onset arm. Rationale and freeze-test justification in `WORKLOG.md`. Nothing else in v1.1 changes; §7.1 keeps the plain binomial CUSUM.

**Razorpay AI Buildathon, Track 03 — AI Revenue Recovery**
**Build window:** 25 Aug – 4 Sep 2026. Deadline 4 Sep. Target submit 3 Sep.

---

## 0. How to use this document

This is a frozen specification. It is the sole source of truth for the build.

**Rules for the implementing agent:**

1. **Do not redesign.** The architecture went through four adversarial review rounds. If you believe something is wrong, write it in `WORKLOG.md` with reasoning and proceed with the spec as written unless it is a fatal correctness bug.
2. **Do not invent Razorpay facts.** Error codes, sources, steps, reasons and API behaviour must come from the official Razorpay documentation, looked up during Day 0. Placeholder values are permitted only if labelled `TODO_VERIFY` and resolved before Day 1 ends.
3. **Honour the leakage invariant** (§1.2). It is enforced by a test.
4. **Honour the freeze protocol** (§13). Once `data/frozen/` exists, nothing upstream of it changes.
5. **Build in schedule order** (§14). Each day has acceptance criteria. Do not start day *N+1* until day *N* passes.
6. Numeric parameters marked *(tunable, dev only)* may be adjusted during development and must be frozen before the holdout is generated. Everything else is fixed.

---

## 1. Thesis and invariants

### 1.1 Thesis

> **How much payment-recovery value is lost to incorrect diagnosis, and how much of that diagnostic regret can progressively stronger attribution recover?**

Not "causal AI improves payment recovery." That is the hypothesis under test, not the claim. Every outcome is a publishable result:

- If L3 helps: *conditional causal refinement closed an additional X% of diagnostic regret beyond statistical attribution.*
- If L3 does not: *at realistic observation density, statistical attribution plus routing topology captured essentially all measurable diagnostic value.*

### 1.2 Invariants — never violate

- **Leakage:** nothing under `src/attribution/`, `src/detector/` or `src/policy.py` may import `environment.yaml`, `mechanisms.yaml`, the true-episode ledger, or any ground-truth field. Enforced by `tests/test_leakage.py`.
- **No ground truth on observables:** `PaymentEvent` and `TelemetryWindow` carry no `root_cause` field. Ground truth lives only in the episode ledger, read only by `src/eval/`.
- **Policy identity:** the recovery policy is byte-identical across baselines B1/B2/B3. Diagnosis is the only varying input.
- **Oracle scope:** the oracle receives the latent cause label only. Never efficacies.
- **Freeze ordering:** dev Monte Carlo → verify → freeze configs → tag → generate holdout. Never the reverse.
- **Money labelling:** all monetary figures are "simulated recovered value" or "simulated incremental recovered value." Never production revenue.

---

## 2. Repository

```
causal-payment-recovery/
├── .gitignore                   # FIRST FILE. .env, .env.*, credentials, data/dev/
├── .env.example                 # committed. real .env never is
├── README.md
├── ARCHITECTURE.md              # diagram, decisions, rejected alternatives
├── PREREGISTRATION.md           # tag + config hashes, written Day 6
├── RESULTS.md
├── WORKLOG.md                   # what broke, how fixed. daily, contemporaneous
├── config/
│   ├── topology.yaml            # METHOD-AWARE routing graph
│   ├── mechanisms.yaml          # 4 mechanisms, real Razorpay code/source/step/reason
│   ├── partition_selector.yaml  # PRE-REGISTERED
│   ├── policy.yaml              # diagnosis → action, plus bounds
│   ├── environment.yaml         # HIDDEN mechanism × action efficacy matrix
│   └── eval.yaml                # thresholds, seeds, matching params
├── src/
│   ├── schema.py
│   ├── simulator/
│   │   ├── volume.py            # Poisson arrivals + diurnal
│   │   ├── failures.py          # multinomial-logit categorical outcomes
│   │   ├── mechanisms.py        # injection
│   │   └── generate.py          # stream + warm-up + episode ledger
│   ├── seasonal.py              # TWO models: overall + per-source
│   ├── detector/cusum.py        # binomial likelihood CUSUM
│   ├── incident.py              # deterministic alert merge
│   ├── attribution/
│   │   ├── l1.py  l2.py  l3.py  ladder.py
│   ├── policy.py                # pure: Diagnosis → Action
│   ├── environment.py           # hidden action-effect sampler
│   ├── execution/razorpay.py    # test-mode calls, REAL/SIM tagged
│   ├── audit.py
│   ├── explain.py               # LLM narration, post-decision only
│   └── eval/
│       ├── matching.py          # Hungarian, attribution-blind
│       ├── baselines.py         # O*, B0, B1, B2, B3
│       ├── metrics.py
│       └── bootstrap.py         # paired bootstrap, McNemar, Wilson
├── data/
│   ├── dev/                     # gitignored, disposable
│   └── frozen/                  # committed: 240 scenarios + warm-up + seeds
├── ui/                          # 4 screens, static read of audit log
└── tests/
    ├── test_leakage.py          # WRITE THIS BEFORE THE SIMULATOR
    └── ...
```

---

## 3. Configuration

### 3.1 `topology.yaml` — method-aware

Razorpay's `error_source` vocabulary differs by method. A single universal `issuer → PSP → network` chain is wrong. Model per-method valid edges only:

```yaml
methods:
  upi:
    sources: [customer_psp, gateway, network, issuer_bank, beneficiary_bank]
    layers: [customer_psp, gateway, network, issuer_bank]
  card:
    sources: [gateway, issuer_bank]
    layers: [gateway, issuer_bank]
  netbanking:
    sources: [gateway, issuer_bank]
    layers: [gateway, issuer_bank]

issuers: [ISSUER_A ... ISSUER_H]        # 8
psps:    [PSP_1 ... PSP_4]              # 4
networks: [NET_1, NET_2]                # UPI only

routing:                                 # deliberately overlapping — shared
  ISSUER_A: {psp: PSP_1, network: NET_1} # infrastructure confounding must be real
  ISSUER_B: {psp: PSP_1, network: NET_1}
  ISSUER_C: {psp: PSP_1, network: NET_2}
  ...
```

Verify the exact source vocabulary against Razorpay docs on Day 0. Do not assume this list is complete.

### 3.2 `mechanisms.yaml` — four mechanisms

Each must carry a **real** Razorpay `code` / `source` / `step` / `reason` combination, looked up from the docs. Use all four fields where they discriminate — `source` alone does not distinguish an issuer outage from an authentication failure spike.

| Mechanism | Dominant source | Method scope | Affected set | Ramp |
|---|---|---|---|---|
| `issuer_degradation` | `issuer_bank` | all methods | one issuer | step |
| `psp_degradation` | `gateway` | all methods | all issuers under one PSP | linear |
| `card_auth_spike` | `issuer_bank` + auth step/reason | **cards only** | one issuer's cards | step |
| `upi_network_degradation` | `network` | **UPI only** | all UPI cells on one network | linear |

`severity ∈ [0.8, 3.0]` log-odds. `duration ∈ [20, 180]` minutes. Overlapping incidents in ~15% of scenarios.

**OOD mechanisms** (2–3, generator-only, never in `mechanisms.yaml` as recognizable classes): e.g. a beneficiary-bank credit-delay pattern, a merchant-side checkout-config regression. The attributor is never designed for these. They exist to test abstention.

---

## 4. Schemas (`src/schema.py`)

```python
from enum import Enum
from dataclasses import dataclass
from datetime import datetime

class Action(str, Enum):
    SAME_RAIL_RETRY       = "SAME_RAIL_RETRY"
    SWITCH_PSP            = "SWITCH_PSP"
    ALTERNATE_METHOD_LINK = "ALTERNATE_METHOD_LINK"
    BACKOFF_REPRESENT     = "BACKOFF_REPRESENT"
    NO_ACTION             = "NO_ACTION"

@dataclass(frozen=True)
class TelemetryWindow:           # PRIMARY analysis unit. 5-minute windows.
    timestamp: datetime
    cell_id: str                 # partition-qualified, e.g. "issuer:ISSUER_A"
    method: str
    n_attempts: int
    failures_by_source: dict[str, int]
    amount_at_risk_paise: int

@dataclass(frozen=True)
class PaymentEvent:              # Razorpay-shaped. Materialised only for demo,
    timestamp: datetime          # audit examples and real execution.
    payment_id: str
    amount_paise: int
    method: str
    issuer: str | None
    psp: str
    network: str | None
    success: bool
    error_code: str | None
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    # NO root_cause field.

@dataclass(frozen=True)
class TrueEpisode:               # ledger. src/eval/ only.
    episode_id: str
    mechanism: str               # incl. "none" and "ood_*"
    start: datetime
    end: datetime
    affected_nodes: list[str]
    affected_methods: list[str]
    severity: float
    # v1.2 (§5.2). Ground truth for the onset/propagation amendment.
    primary_cell: str | None     # ground-truth root; None for "none"
    onset_arm: str               # "propagating" | "simultaneous"
    onset_offsets: dict[str, int]  # cell → Δ in windows
    coupling_beta: float         # β_m; 0.0 on the simultaneous arm

@dataclass(frozen=True)
class Diagnosis:
    incident_id: str
    cause_node: str | None       # None ⇒ UNKNOWN
    mechanism_family: str | None
    level_used: str              # L1 | L2 | L3 | ABSTAIN
    confidence: float
    l3_eligible: bool
    l3_abstain_reason: str | None
    evidence: dict
```

**Counts are sufficient statistics** for the binomial GLM, the CUSUM and source-rate attribution. Do not materialise seven warm-up days of transaction-level events across 240 scenarios — that is a storage and runtime problem for no analytical gain. Note in `RESULTS.md` that recovered value derives from `amount_at_risk_paise` per window, not per-payment amounts.

---

## 5. Simulator

### 5.1 Volume

5-minute windows, 288/day. Arrivals `n_t ~ Poisson(Λ · v(t))`:

```
v(t) = 1 + a₁sin(2πt/288) + b₁cos(2πt/288) + a₂sin(4πt/288) + b₂cos(4πt/288)
```

clipped positive. Set `Λ` so a median cell sees **40–60 attempts/window** — above the exposure gate, but not so far above that power stops being a real constraint. Amounts lognormal; `amount_at_risk_paise` = summed amounts of failed attempts.

### 5.2 Failure generation — categorical, NOT competing risks

**Critical.** Independent per-source Bernoulli draws with a precedence tie-break would let high-precedence sources systematically suppress lower ones at high failure rates — silently baking a diagnostic artifact into ground truth, which the attributor then reads as signal. Use a single categorical outcome per attempt.

For cell *p*, window *t*, over sources *s* **valid for that method**:

```
η_s(p,t) = θ_{p,s} + seasonal_s(t)
         + Σ_m γ_{m,p} · ramp_m(t − Δ_{m,p}) · 1[p ∈ N_m ∧ s = s_m]
         + Σ_m β_m · Σ_{q ~ p} w_pq · d_{q,s}(t−1) · 1[p ∈ N_m ∧ s = s_m]

where  d_{q,s}(t) = logit(q̂_{q,s}(t)) − logit(baseline_{q,s}(t))   # DEVIATION

P(source = s) = exp(η_s) / (1 + Σ_j exp(η_j))
P(success)    = 1        / (1 + Σ_j exp(η_j))
```

Then per cell-window: `counts ~ Multinomial(n_t, probabilities)`. Exactly one observable outcome per attempt. No precedence rule anywhere.

**Onset, heterogeneity and propagation (v1.2 amendment).** v1.1 applied a single shared `ramp_m(t)` identically to every cell in `N_m` at the same instant. That makes onset simultaneous and the mechanism a constant common factor across affected cells — so temporal precedence among cells is zero *by construction*, L3's root criterion (§8.3) has no estimand, and `B3 − B2 = 0` is guaranteed before any code is written. See `WORKLOG.md`, 26 Aug. The generator must therefore carry:

1. **Onset order.** Each mechanism designates a **primary cell**, recorded in the episode ledger as the ground-truth root. Every other affected cell activates at `t0 + Δ_{m,p}`, with `Δ_{m,p} ~ Discrete{0, 1, 2, 3, 4}` windows. The primary cell has `Δ = 0`.
2. **Heterogeneous severity.** `γ_{m,p} = γ_m · U(0.6, 1.4)`, drawn per affected cell. The shift is no longer a constant common factor.
3. **Cascade coupling.** The lagged cross-cell term above runs over topologically linked cells `q ~ p` (§3.1 routing), with `w_pq` the routing weight and the lagged rate smoothed per §6. `β_m` is **highest for `psp_degradation`** — retry pressure genuinely shifts load across the cells behind one gateway — and **lowest (≈0) for `card_auth_spike`**, which is locally contained to one issuer's cards.

   The coupling carries the **deviation** `d_{q,s}(t−1)`, not the raw lagged logit. Coupling on the raw logit would add a constant offset during calm periods — every cell would sit permanently above its own seasonal baseline, the seasonal fit of §6 would absorb it, and the detector's null would silently shift. On the deviation the term is zero whenever linked cells are at baseline, so propagation exists only while something is actually wrong. *(Correction applied Day 1; the v1.2 draft wrote the raw form.)*
4. **Mixture — pre-registered, never tuned after seeing results.** **60% propagating** (`Δ_{m,p}` drawn as above, `β_m > 0`) and **40% simultaneous** (`Δ_{m,p} = 0` for all *p*, `β_m = 0`). `TrueEpisode` records which arm the scenario drew.

**Grounding — every propagation path must be a real payment behaviour, not a shape PCMCI finds easy.** A PSP degradation surfaces first on the highest-share issuer or on whichever issuer carries the tightest timeout configuration; retry storms shift load across rails; UPI issues reach beneficiary banks at different rates. Do not add propagation structure because it improves L3's numbers — that is the mirror image of the circularity the four review rounds removed.

The simultaneous arm stays in the mixture deliberately. An L3 null on that subset is the correct and expected result, and reporting it (§12.5) is what keeps the propagating-arm result honest.

### 5.3 Streams

Every scenario stream = **7 clean warm-up days** (no incidents) followed by the evaluation period. Seasonal parameters are fit on warm-up only and frozen before the incident window opens.

---

## 6. Seasonal baseline (`seasonal.py`)

Fit **two** models per cell, on warm-up only, binomial GLM weighted by `n_t`:

1. **Overall failure baseline** — `P(any failure | cell, t)`, consumed by the detector.
2. **Per-source baselines** — `P(source = s failure | cell, t)`, consumed by L3.

Basis:

```
logit(p̂) = β₀ + β₁sin(2πt/288) + β₂cos(2πt/288) + β₃sin(4πt/288) + β₄cos(4πt/288)
```

Add day-of-week terms only if 7 days supports it — check, don't assume.

**Smoothing:** wherever a rate logit is formed manually, use `q̂ = (x + 0.5)/(n + 1)` so a window with zero source-failures doesn't produce `logit(0) = −∞`.

---

## 7. Detector and incident merge

### 7.1 Binomial likelihood CUSUM (`detector/cusum.py`)

Per cell. Window *t*: `n_t` attempts, `x_t` failures, seasonal baseline `p₀(t)`, alternative `p₁(t) = min(0.95, κ·p₀(t))`, `κ = 1.6` *(tunable, dev only)*.

```
Λ_t = x_t·ln(p₁/p₀) + (n_t − x_t)·ln((1−p₁)/(1−p₀))
S_t = max(0, S_{t−1} + Λ_t)
alert when S_t > h
```

`n_t` enters directly, so 50% of 2 attempts barely moves `S_t` while 50% of 2,000 moves it hard. Calibrate `h` on warm-up to ~1 false alarm per cell per 48h *(tunable, dev only)*; record the procedure in `RESULTS.md`.

### 7.2 Incident merge (`incident.py`)

Deterministic. Two alerts merge iff **all three**:

1. within 15 min of each other,
2. cells linked in the routing topology (shared PSP or network, method-valid),
3. compatible dominant `error_source` family.

An incident ends when all constituent CUSUMs stay below `h` for 3 consecutive windows.

---

## 8. Attribution ladder

### 8.1 L1 — deterministic

Exactly one cell alerting in the partition, its residual log-odds shift exceeds threshold, all topological peers within their Wilson band. Return that cell, `confidence = 0.9`.

### 8.2 L2 — statistical + routing topology

1. Per-cell residual shift `Δ_p` with Wilson CI.
2. Candidates: each alerting cell individually, plus each topology node covering ≥2 alerting cells.
3. For each topology candidate: pooled two-proportion test, cells under the node vs. cells not under it, conditioned on the dominant error source.
4. Winner must beat runner-up by a pre-registered margin, else return **UNKNOWN**.
5. `mechanism_family` from the `source`/`step`/`reason` signature.

**Topology mapping lives here, not in L3.** The shared-PSP explanation comes from known routing metadata. Causal discovery is never permitted to posit a latent infrastructure node — that would violate causal sufficiency and produce confident spurious edges.

### 8.3 L3 — conditional causal refinement

**Partition selection is pre-registered** (`partition_selector.yaml`), committed before evaluation:

| Dominant source signature | Permitted partition |
|---|---|
| `issuer_bank` dominant | issuer |
| `gateway` dominant | PSP |
| `customer_psp` dominant | PSP |
| method-local auth pattern | method |
| ambiguous / two signatures | **no L3 run** |

No runtime "pick whichever graph looks cleanest."

**Preprocessing:** deseasonalize using warm-up-fitted parameters. Variable = residual log-odds of the **dominant source only** (one error-source family per run — source rates are coupled through the overall failure rate, and compositional-data machinery is out of scope) in cell *p*, with **leave-one-out** global correction:

```
Ṽ_p(t) = logit(q̂_p(t)) − logit(q̂_{−p}(t)) − seasonal_residual_p(t)

V_p(t) = Ṽ_p(t) / SE_p(t)        # variance-standardized (v1.2 amendment)
```

`q̂_{−p}` excludes cell *p*'s own attempts. Including the cell in its own baseline mechanically couples the features.

**Variance standardization (v1.2 amendment) — mandatory.** `SE_p(t)` is the estimated standard error of `Ṽ_p(t)` under the seasonal null, i.e. the delta-method binomial standard error of the smoothed logit at cell *p*'s window exposure `n_{p,t}` (§6 smoothing applies), combined with the leave-one-out term's.

Without it, cell volume drives edge significance. A high-volume cell has a tighter CI on the *same* log-odds shift, so it crosses significance earlier and PCMCI reads that as precedence — L3 would systematically nominate the busiest cell in the partition as the root. **Block bootstrap cannot catch this**: cell size is constant across resamples, so the artifact is perfectly stable and the ≥70% stability check would *confirm* the wrong root rather than reject it.

This is independent of the §5.2 propagation amendment and is required regardless of it.

**Dev-pool diagnostic, required before the freeze:** on the dev pool, correlate the recovered root's volume rank against chance. Report the correlation in `RESULTS.md`. A systematic association between "recovered root" and "highest-volume cell" invalidates the L3 result and must be resolved before Day 6.

**Gates — all must pass, else abstain to L2:**

- exposure: ≥30 attempts per cell per window across the span
- temporal: **≥288 windows (24h)** of usable history. *One value per 5-min window means T ≈ 288 temporal samples, not 288×30 — this gate is about T, not transaction count.*
- matrix size: **5–8 variables** (not 12 — PC-step power degrades fast as the parent set grows at T≈288)
- partition unambiguous per the table above

**PCMCI:** `tigramite`, ParCorr, τ_max = 6, α = 0.05. **Apply BH-FDR to the final MCI edge set** before root selection — with 5–8 variables and 6 lags this is many tests, and raw `p < .05` link selection is indefensible.

**Root selection:** among shortlisted observed nodes, the node with no significant parents inside the set and earliest significant outgoing edges. **Stability:** block bootstrap, B = 100; the same root must win in ≥70% of resamples.

**Abstain when:** any gate fails; no stable edges survive FDR; root unstable under bootstrap; multiple roots with insufficient separation; result contradicts routing constraints. **Cycles are NOT an abstention trigger** — lagged feedback (`X_t → Y_{t+1}`, `Y_t → X_{t+1}`) is legitimate in a time-series graph.

**L3's permitted claims:** temporal precedence among observed co-deviating cells; residual structure remaining after L2's topology attribution (evidence of a second mechanism or a cascade). It may **not** posit an unobserved root.

**Ladder rule:** only L3 abstains. L3 abstention never erases a valid L2 diagnosis; the system still acts on L2.

*Rejected alternative — document in `ARCHITECTURE.md`:* crossed (issuer × PSP) cells would be genuinely disjoint and would make both effect levels recoverable, but 8×4 = 32 cells collapses per-cell volume exactly where power already binds.

---

## 9. Recovery policy (`policy.py`)

Pure function `Diagnosis → Action`. Identical across B1/B2/B3.

| Diagnosis | Action |
|---|---|
| `issuer_degradation` | `ALTERNATE_METHOD_LINK` |
| `psp_degradation` | `SWITCH_PSP` |
| `card_auth_spike` | `ALTERNATE_METHOD_LINK` (never retry — retry re-triggers auth) |
| `upi_network_degradation` | `BACKOFF_REPRESENT` |
| `UNKNOWN` | `NO_ACTION` + escalate |
| no degradation | `NO_ACTION` |

**Bounds, logged whenever they fire:** max 2 retries per payment; 10-min cooldown; no customer contact 22:00–08:00 IST; per-incident spend ceiling; global kill switch.

---

## 10. Hidden action-effect environment (`environment.py`)

**This is the component that makes the thesis measurable.** It must be keyed by **true mechanism × executed action** — not by "the right action vs. blind retry." When B2 misdiagnoses a PSP degradation as an issuer degradation, the policy executes `ALTERNATE_METHOD_LINK`, and attribution regret is undefined unless the environment knows the consequence of *that action under the true mechanism*.

Full cross-product, efficacies sampled per scenario from frozen ranges the attribution and policy layers never see.

**Recovery probability ranges** *(starting values; validate flips in dev Monte Carlo, then freeze)*:

| True mechanism | `SAME_RAIL_RETRY` | `SWITCH_PSP` | `ALTERNATE_METHOD_LINK` | `BACKOFF_REPRESENT` | `NO_ACTION` |
|---|---|---|---|---|---|
| `issuer_degradation` | U(.05,.45) | U(.10,.40) | U(.35,.85) | U(.25,.60) | U(.10,.30) |
| `psp_degradation` | U(.10,.40) | U(.40,.80) | U(.25,.60) | U(.20,.55) | U(.10,.30) |
| `card_auth_spike` | U(.02,.30) | U(.05,.35) | U(.25,.60) | U(.15,.45) | U(.08,.25) |
| `upi_network_degradation` | U(.15,.55) | U(.15,.45) | U(.20,.60) | U(.30,.70) | U(.12,.35) |
| `none` | — | — | — | — | — |
| `ood_*` | U(.05,.35) | U(.05,.35) | U(.10,.45) | U(.15,.50) | U(.20,.55) |

**Action costs** (added abandonment fraction, applied regardless of mechanism): `SAME_RAIL_RETRY` U(.00,.05); `SWITCH_PSP` U(.00,.20); `ALTERNATE_METHOD_LINK` U(.05,.25); `BACKOFF_REPRESENT` U(.02,.15); `NO_ACTION` 0.

**The `none` row.** Nothing is at risk, so recovery is undefined — but intervening still costs. Apply the action cost against the touched volume plus a fixed customer-contact penalty. Without this row, false-intervention rate is a number with no money attached and "an agent that always acts is dangerous" never enters the regret decomposition.

**The `ood_*` rows.** `NO_ACTION` is deliberately competitive, so abstention has a measurable payoff and escalating-over-guessing shows up in rupees rather than only in principle.

### 10.1 Simulated recovered value — the formula (v1.3, decided Day 3)

v1.1–v1.2 gave recovery probabilities and action costs but never combined them,
leaving the primary outcome variable undefined. Fixed:

```
value(incident, action) =
      at_risk_paise      × p_recover(true_mechanism, action)
    − touched_paise      × cost(action)
    − contact_penalty_paise × 1[action is customer-contacting]
```

- `at_risk_paise` — summed `amount_at_risk_paise` over the incident's cells and
  windows. Only failed attempts are at risk.
- `touched_paise` — **all** traffic the action passes through, successful
  attempts included. `SWITCH_PSP` reroutes an entire gateway's flow; the
  abandonment it adds falls on customers who would have paid fine.
- `cost(action)` — the added-abandonment fraction of §10.

The two bases differ deliberately. Charging cost only against `at_risk` would
make a false intervention nearly free, and "an agent that always acts is
dangerous" would never appear in rupees — which §10 already says is the point
of the `none` row. Under this formula a false alarm on a healthy PSP costs
`touched × cost` with nothing to recover, so it is straightforwardly negative.

`NO_ACTION` has zero cost, zero touched volume and no penalty, so its value is
zero on every row except through the recovery column of §10's table.

**Falsifiability requirement:** ranges must overlap enough that the optimal action genuinely flips between draws, and `ALTERNATE_METHOD_LINK`'s higher cost must sometimes make acting worse than waiting. **Verify this in the dev Monte Carlo pool — never by inspecting the frozen holdout** (see §13).

**Sensitivity sweep:** pessimistic / base / optimistic efficacy regimes over the same 240 frozen scenarios. The result counts only if the policy beats B0 in all three.

---

## 11. Execution (`execution/razorpay.py`)

Every action tagged `REAL` or `SIMULATED` in the audit record and rendered as such in the UI.

- **REAL:** Standard Payment Link creation in test mode. Webhook receipt.

  > **v1.3 correction (Day 4, verified against the docs).** v1.1 said "with method customization — this makes `ALTERNATE_METHOD_LINK` a genuine live demo rather than a simulated one." **That premise is false.** The Payment Links create API exposes no request field for restricting which methods a customer may use; `method` appears only in the *response*, reporting what was eventually used. `options.checkout.method` is accepted and silently ignored — a failure mode that looks like success.
  >
  > The link is genuinely REAL. The **rail restriction on it is not**, and is reported as `rail_restriction_enforced: false` rather than claimed. A REAL link whose restriction did not apply must never be reportable as a rail-restricted demo.
  >
  > To make the restriction genuine, use **Razorpay Checkout (JS)** on a small local page, where `config.display.hide` / method blocks are honoured. That is a better demo anyway: the viewer watches the card option disappear. Decision pending — see WORKLOG 27 Aug.
- **SIMULATED:** PSP/gateway rerouting, rail switching, retry effectiveness.

Test mode caps Payment Links (~30 per business). Create **one or two** live demo links; build no reuse logic against the cap. Verify current limits and the test-mode support for the specific endpoint against the docs before relying on any of this.

`.env` gitignored from the first commit; only `.env.example` tracked. The Razorpay test secret must never touch git history.

---

## 12. Evaluation

### 12.1 Frozen set

**240 frozen evaluation scenarios** (not "incidents" — the 48 nulls are deliberately not incidents, and mixing them into an incident denominator is wrong):

- 160 known-mechanism (40 each × 4)
- 48 null / no-op
- 32 OOD / adversarial

Plus warm-up history per stream. Seeds recorded, files committed.

### 12.2 Matching — attribution-blind (`eval/matching.py`)

Score each (predicted, true) pair on **temporal IoU × affected-topology overlap only**. Mechanism-family compatibility is **excluded** from the primary matcher — including it lets a correctly-detected incident with a wrong diagnosis fail to match, becoming one FP + one FN instead of one TP plus one attribution error, which contaminates detection recall with attribution quality and double-penalises the same mistake.

Maximum-weight bipartite matching (Hungarian), minimum threshold 0.3. Then: matched → TP, unmatched predicted → FP, unmatched true → FN. Mechanism compatibility survives as a debug column only.

No fractional credit in headline metrics. A split prediction = one match + one FP, tagged `SPLIT_ERROR`. A merge = one match + one FN, tagged `MERGE_ERROR`. Both go in the failure list.

Attribution accuracy is computed **only over matched incidents where attribution applies**.

### 12.3 Baselines (`eval/baselines.py`)

Detector and policy identical across B1/B2/B3.

| | Detection | Cause | Policy |
|---|---|---|---|
| O* | oracle | oracle | same |
| B0 | — | — | always `SAME_RAIL_RETRY` |
| B1 | same detector | oracle | same |
| B2 | same detector | L1/L2 | same |
| B3 | same detector | L1/L2/L3 | same |

```
Detection regret        = O* − B1
Attribution regret      = B1 − B3
L1/L2 attribution gap   = B1 − B2
Value added by L3       = B3 − B2
```

### 12.4 Statistics (`eval/bootstrap.py`)

All paired, same frozen scenarios. McNemar for attribution correctness. Paired bootstrap (B = 2000, percentile) for accuracy delta and recovered-value delta. Wilson intervals for standalone proportions.

**L3 enters the headline only if the paired improvement over B2 is positive with a CI excluding zero AND materially reduces attribution regret.** No fixed percentage-point threshold — at ~160 known-mechanism scenarios a 3-point difference is easily noise. CI crossing zero ⇒ reported as inconclusive.

### 12.5 Report (`RESULTS.md`)

Detection recall; false-alarm rate; incident precision/recall; **attribution accuracy at coverage** (never accuracy alone — a system abstaining on 80% posts beautiful conditional accuracy while being useless); known-class abstention rate; OOD recognition rate; L3 eligibility rate; L3 abstention rate with reasons; false-intervention rate on nulls; simulated recovered value; full regret decomposition; paired deltas with CIs; split/merge counts; sensitivity sweep; **explicit failure list**.

**L3 split by onset arm (v1.2 amendment).** Report L3 accuracy, eligibility and abstention **separately for the propagating and simultaneous arms** of §5.2's mixture, never pooled.

- A null on the **simultaneous** subset is expected and correct — there is no temporal precedence to recover, and L3 abstaining there is the system behaving as designed.
- A null on the **propagating** subset is the real finding: structure was injected and L3 failed to recover it.

Pooling the two arms would let the simultaneous subset dilute a genuine propagating-arm result, or let a propagating-arm result mask the fact that L3 is firing on the arm where it should abstain. Also report the root-volume-rank diagnostic from §8.3.

---

## 13. Freeze protocol

**Order is mandatory. Violating it is tuning on test.**

```
1. DEV MONTE CARLO — large disposable pool in data/dev/
2. Verify action rankings genuinely flip across sampled efficacies
3. Adjust efficacy ranges, thresholds, κ, h procedure, margins
4. FREEZE config/: environment.yaml, policy.yaml, eval.yaml,
   partition_selector.yaml, mechanisms.yaml, topology.yaml
5. Write PREREGISTRATION.md; git tag
6. THEN generate data/frozen/ — 240 scenarios, untouched
7. Run baselines. Never alter anything from steps 3–5 afterwards.
```

`PREREGISTRATION.md` records the commit/tag plus hashes of every evaluation-critical constant: policy, environment efficacy ranges, partition selector, L1 thresholds, L2 winner margin, CUSUM κ and h calibration procedure, incident merge thresholds, matching threshold, L3 gates, τ_max, PCMCI significance procedure including FDR, bootstrap block size and stability threshold, random seeds, metric definitions.

**Discipline during implementation:** whenever tempted to change an evaluation parameter because results look bad, write the proposed change in `WORKLOG.md` first and ask — *would I make this same change if the result currently looked excellent?* If no, don't touch the frozen path.

---

## 14. Schedule and acceptance criteria

| Day | Date | Deliverable | Done when |
|---|---|---|---|
| 0 | Tue 25 Aug | `.gitignore` → repo → first commit. `WORKLOG.md`. Razorpay docs → `mechanisms.yaml` (real code/source/step/reason), method-aware `topology.yaml`. `schema.py` incl. `Action` enum. `tests/test_leakage.py`. | Leakage test passes on an empty tree; four mechanisms carry real documented field combinations; no `.env` in git |
| 1 | Wed 26 Aug | Simulator: volume + diurnal + multinomial-logit failures + 4 mechanisms + OOD + warm-up + episode ledger | A stream renders as `TelemetryWindow` counts; injected incidents visible by eye; ledger separate from observables |
| 2 | Thu 27 Aug | `seasonal.py` (both models, smoothing), binomial CUSUM, `h` calibration, incident merge | Detector fires on injected incidents; false-alarm rate on clean warm-up near target |
| 3 | Fri 28 Aug | L1, L2 + routing topology, `policy.py`, `audit.py` | A diagnosis and a bounded action are produced end to end for one scenario |
| 4 | Sat 29 Aug | **Thin end-to-end run.** `environment.py` with full cross-product. One real test-mode Payment Link. | Pipeline runs start-to-finish on one scenario and emits an audit trail. **KILL GATE** |
| 5 | Sun 30 Aug | Eval harness: Hungarian matcher, O*/B0/B1/B2, metrics | B0/B1/B2 produce numbers on the dev pool |
| 6 | Mon 31 Aug | Dev Monte Carlo → verify flips → **freeze configs → `PREREGISTRATION.md` → git tag → generate `data/frozen/`** → run B0/B1/B2 | Tag exists and predates `data/frozen/`. Real numbers exist |
| 7 | Tue 1 Sep | L3: deseasonalized residuals, partition selector, gates, PCMCI + FDR, bootstrap stability | L3 returns a root or a named abstention reason on eligible scenarios. **KILL GATE** |
| 8 | Wed 2 Sep | B3, paired stats, sensitivity sweep, **L3 headline decision**, LLM explainer, 4-screen UI | Ablation table with CIs exists; headline decision recorded in `WORKLOG.md` |
| 9 | Thu 3 Sep | `README`, `ARCHITECTURE`, `RESULTS`, `WORKLOG` final. Record video. **Razorpay application form. Submit** | Submitted |
| 10 | Fri 4 Sep | Buffer | — |

### Kill rules — pre-committed

- **End of Day 4:** if the thin loop isn't running, cut execution entirely and pivot to **Track 02** with detector + attributor as a degradation-detection submission. Every component built so far transfers.
- **End of Day 7:** if L3 has no numbers, run B0–B2 only and report why causal attribution was infeasible at this observation density. That is a legitimate finding, not a failure.

---

## 15. Day 0 checklist

1. ~~Submit the Razorpay application form.~~ **Moved to Day 9** — the form is
   submitted once the program and demo video are ready. It depends on the
   finished artefact and gates nothing upstream.
2. `.gitignore` **before any commit** — `.env`, `.env.*`, credentials, `data/dev/`.
3. `git init`, create the tree, first commit.
4. `WORKLOG.md`, first entry.
5. Razorpay error-code docs → lock the four mechanisms' `code`/`source`/`step`/`reason`.
6. Method-aware `topology.yaml`.
7. `schema.py` — `TelemetryWindow`, `PaymentEvent`, `TrueEpisode`, `Diagnosis`, `Action` enum.
8. `tests/test_leakage.py` — before the simulator exists.
9. **Stop.** Do not start L1/L2/PCMCI tonight. Day 0 succeeds if the experimental contract is correct.

---

## 16. Video (5:00) and panel

- 0:00–0:30 — twelve dashboard segments are red. Which one is upstream?
- 0:30–1:15 — architecture. *"The LLM never decides why a payment failed."*
- 1:15–3:00 — two live incidents: PSP degradation → `SWITCH_PSP` succeeds; card auth spike → **correctly declines to retry**. The refusal is the money shot.
- 3:00–4:15 — regret decomposition and ablation with CIs.
- 4:15–5:00 — limitations: synthetic data, simulated rerouting, L3 abstention rate, what it got wrong.

**Panel prep.** Be ready to justify: binomial CUSUM over a learned detector; PCMCI over Granger; a decision table over an LLM policy; why topology mapping sits in L2 not L3; why crossed cells were rejected; what one PCMCI variable is and what one time step is; why the matcher is attribution-blind. Know the leakage invariant and the pre-registration tag cold — those two are what make the numbers believable.
