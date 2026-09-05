# RESULTS

All monetary figures are **simulated recovered value**, never production
revenue (SPEC §1.2).

Frozen set: 240 scenarios (160 known-mechanism, 48 null, 32 OOD), generated
**after** tag `prereg-v1`, which was created with `data/frozen/` verifiably
empty. Config and code hashes in `PREREGISTRATION.md`, including a post-tag
amendment recording a metric that was **mislabelled** in an earlier draft of
this file (see §2.1). Nothing below was tuned against these scenarios.

Status: B0/B1/B2 complete. L3 (B3) lands Day 7, so B3 equals B2 by construction
and the L3 term is exactly zero.

---

## 1. Headline

> **The system produced ₹2.02M more simulated net value per scenario than
> blind retry, while imperfect attribution cost ₹89,843 per scenario relative
> to oracle diagnosis.**

The second clause is the one that matches the pre-registered thesis, and it is
the harder result to attack. B1 and B3 hold the detector and the policy fixed,
so ₹89,843 is a clean causal estimate of **value lost specifically to
diagnosis**. The ₹2.02M figure is the value of the *entire system* against a
deliberately naive comparator and should never be described as "the value of
diagnosing before acting."

## 2. The error ladder

Each rung holds everything below it fixed, so the gaps are attributable.

```
  ₹473,448   oracle detection + oracle diagnosis, FROZEN §9 POLICY
      |
      |  detection regret            ₹170,556   [+126,240, +219,037]
      v
  ₹302,892   real detector + oracle diagnosis          (B1)
      |
      |  attribution regret           ₹89,843   [ +44,560, +160,333]
      v
  ₹213,049   real detector + L1/L2 diagnosis           (B2 = the system)
```

All three CIs exclude zero (paired bootstrap, B = 2000, percentile).

B2 captures **45.0%** of the oracle-detection/oracle-diagnosis value achievable
**under the frozen policy** (213,049 / 473,448).

Standing **beside** the ladder, not inside it:

| comparator | value/scenario | what it is |
|---|---|---|
| blind retry (B0) | **−1,803,750** | naive comparator, not an error term |
| hindsight action ceiling | 648,805 | requires foreknowledge; see §2.1 |

### 2.1 The ₹473,448 benchmark, and a correction

**₹473,448 is not a "perfect-information ceiling."** It is the
**oracle-detection + oracle-diagnosis value under the frozen §9 policy**. The
policy itself can be suboptimal, so "perfect information" overstates it.

**An earlier draft of this file reported a "policy regret" of ₹175,357. That
label was wrong and is withdrawn.** The quantity is computed by
`environment.best_action`, which receives the scenario's **drawn** efficacies
and returns the argmax — random per-scenario values no deployable system can
observe at decision time. It is therefore a **hindsight action-selection gap**:
an upper bound on what any action rule could achieve *with foreknowledge of the
draws*.

Genuine policy regret would need a comparator action rule selected on dev data
before the frozen run, using only decision-time information. **This build has
none, so no policy-regret figure is quoted.**

The corrected quantity — **hindsight action-selection gap = ₹175,357/scenario**
— is still worth reporting: it says the frozen §9 table leaves substantial value
on the table relative to an unattainable oracle, which motivates a learned or
context-sensitive policy as future work. It is not evidence that the policy is
*badly* specified, because nothing could reach that ceiling.

No computed value changed in making this correction; only two docstrings were
edited, verified by AST comparison. See `PREREGISTRATION.md` §A1.

## 3. Detection

| metric | value | 95% CI (Wilson) |
|---|---|---|
| recall | 53.2% | [46.6%, 59.7%] |
| precision | **5.4%** | [4.5%, 6.4%] |
| TP / FP / FN | 117 / 2050 / 103 | |

Precision is poor and it is a **consequence of the pre-registered selection
rule**, not an accident. `h` was chosen on the dev pool to maximise B2's
simulated recovered value (§13 step 3); that optimum sits at low precision
because the asymmetry is real — missing an outage forfeits the whole recovery,
while a false intervention on a healthy segment costs only the contact penalty.

## 4. The main weakness: two distinct defects on the null scenarios

These are **separate failures** and were conflated in an earlier draft.

| on the 48 null scenarios | value | 95% CI |
|---|---|---|
| **false-ALERT rate** — detector raised ≥1 incident | **100%** (48/48) | [92.6%, 100%] |
| mean incidents raised per null scenario | 8.0 | |
| **false-INTERVENTION rate** — B2 executed ≥1 action | **91.7%** (44/48) | [80.4%, 96.7%] |
| same, with oracle diagnosis (B1) | **0%** (0/48) | |
| B2 net on nulls | −15,099 | |

A null scenario is a **2-day evaluation window across 24 cells**, not a day.
The metric is: *44 of 48 null scenarios saw at least one automated
intervention.*

**Defect 1 — the detector is noisy.** It raises 8 spurious incidents per null
scenario. A 100% false-alert rate is a real deficiency and is not excused by
what happens downstream.

**Defect 2 — attribution converts noise into confident action.** B1 sees
*identical* detector output and intervenes zero times; B2 converts 2.19 of the
8 into executed actions. The mechanism:

```
null fluctuation
      -> CUSUM alert
      -> single deviating cell in the partition
      -> L1 reads isolation as strong evidence
      -> high-confidence diagnosis (confidence 0.9)
      -> unnecessary intervention
```

The design assumption in §8.1 was *isolated deviation implies easier
attribution*. The frozen test found that **isolated random deviation also
implies falsely confident attribution**. L1 has no notion of "this deviation is
too small to be worth a confident answer" — it checks that peers are quiet, and
on a null scenario they are.

### 4.1 Accuracy alone concealed this

B2's attribution accuracy is **79.5%**, which reads respectably. That number is
computed over matched incidents where attribution applies — i.e. essentially the
positive class. It says nothing about behaviour on the negative class, where the
system is operationally unacceptable.

**A system can be 79.5% accurate and still unfit to deploy because of how it
behaves when nothing is wrong.** This is precisely why §12.5 forbids reporting
accuracy without coverage, and this result argues the requirement should extend
further: accuracy, coverage, *and* negative-class behaviour.

### 4.2 Why this was not fixed

Re-selecting `h` under a false-intervention constraint would be tuning on the
frozen set. §13's discipline question — *would I make this same change if the
result currently looked excellent?* — answers itself: at 10% I would have
shipped `h = 5.5` without a second thought. The tag stands.

**The lesson is about the pre-registration, not the detector.** Selecting on
expected value alone was under-specified. A constrained rule — maximise value
*subject to* a false-intervention rate below a stated threshold — would have
been the better pre-registration, and the dev sweep indicates it would have
chosen a higher `h`. That belongs in any follow-up's pre-registration.

**L3 will not be used to repair this.** L3 runs exactly as pre-registered. If it
does not touch these L1 false interventions — and there is no reason it should,
since L1 fires before L3 is consulted — that will be stated plainly.

## 5. On the blind-retry baseline

B0 at −₹1,803,750 is a large number and invites the objection that it is a
strawman. The objection has force and the framing is corrected here.

Under `true_mechanism = "none"`, **every action including `SAME_RAIL_RETRY` has
recovery probability exactly 0.0** (`config/environment.yaml`). Ordinary
background failures are modelled as entirely non-retryable. That is a strong
assumption: real transient failures — momentary timeouts, transient issuer
declines — are sometimes retryable, and a production retry system with backoff
and eligibility rules would recover some of them.

**B0 is therefore a deliberately naive retry-every-failure baseline, not a
representative optimised production retry system.** The frozen environment is
unchanged; only the description is corrected. The B0 gap is reported as
supporting context and is deliberately **not** the headline.

The credible experiment is **B1 versus B3**, which holds detector and policy
fixed and is not vulnerable to this objection.

## 6. Detector calibration (SPEC §7.1)

`κ = 1.6`, `h = 5.5`, chosen on the dev pool by B2 net value across a paired
sweep — every `h` evaluated on identical streams and identical fitted baselines.

| h | B2 net Rs | recall | precision |
|---|---|---|---|
| 4.0 | 382,518 | 56.5% | 1.6% |
| **5.5** | **400,084** | 48.4% | 5.3% |
| 7.0 | 373,052 | 46.8% | 14.8% |
| 9.0 | 249,435 | 38.7% | 22.9% |
| 22.0 | −41,432 | 12.9% | 11.3% |

Flat between 4.0 and 7.0, so not a knife edge.

Two detector corrections were made on Day 5, both standard practice and both
before the freeze: a **bounded CUSUM** (an unbounded statistic left alerts
running ~3× longer than the incidents causing them) and a **changepoint-based
alert start** (so detection delay is not baked into the reported span). Together
these moved dev-pool recall from 3.2% to ~50%.

**Day-of-week terms: checked and rejected.** BIC 2270 (Fourier alone) vs 2311
(with 6 DOW dummies). With 7 warm-up days each level rests on a single day.

## 7. Falsifiability of the environment (SPEC §10)

Verified on the dev pool before the freeze. The optimal action must genuinely
flip between draws or diagnosis quality would be unmeasurable.

| true mechanism | policy prescribes | prescribed action optimal in |
|---|---|---|
| `issuer_degradation` | `ALTERNATE_METHOD_LINK` | 64% of draws |
| `psp_degradation` | `SWITCH_PSP` | 66% |
| `card_auth_spike` | `ALTERNATE_METHOD_LINK` | 53% |
| `upi_network_degradation` | `BACKOFF_REPRESENT` | 49% |

Each prescribed action is the modal optimum while the optimum still flips in a
third to half of draws.

## 8. Limitations

1. **B0 is a naive comparator** (§5) — not an optimised production retry system.
2. **No policy-regret figure exists** (§2.1). The available quantity is a
   hindsight ceiling requiring foreknowledge.
3. **100% false-alert rate and 91.7% false-intervention rate on nulls** (§4).
   Operationally disqualifying as it stands.
4. **Selection rule was under-specified** — value alone, with no constraint on
   negative-class behaviour (§4.2).
5. **Synthetic data throughout.** Propagation delays, efficacies and amounts are
   modelled, not measured.
6. **Rail restriction is simulated for Payment Links** — genuine only on the
   Checkout demo page (§11 correction, `WORKLOG` 27 Aug).
7. **`card_auth_spike` is modelled as `issuer_bank`-sourced**; production mixes
   ACS outages with genuine customer error under source `customer`.

## 9. Sensitivity sweep (SPEC §10)

Same 240 frozen scenarios, efficacies rescaled per regime. §10 requires the
policy to beat B0 in **all three**.

| arm | pessimistic | base | optimistic |
|---|---|---|---|
| oracle det + oracle diag | 339,560 | 473,448 | 602,909 |
| B1 | 216,830 | 302,892 | 386,064 |
| **B2 (the system)** | **120,149** | **213,049** | **300,314** |
| B0 (blind retry) | −2,426,163 | −1,803,750 | −1,290,576 |

**B2 beats B0 in all three regimes.** §10's requirement is met.

### Attribution regret is stable across regimes

| regime | attribution regret | 95% CI | excludes zero |
|---|---|---|---|
| pessimistic | ₹96,681 | [+48,278, +169,413] | yes |
| base | ₹89,843 | [+44,560, +160,333] | yes |
| optimistic | ₹85,750 | [+40,999, +157,070] | yes |

The headline quantity moves by under 12% across the full sweep and its CI
excludes zero in every regime. That is the robustness check the headline needed:
₹89,843 is not an artefact of one efficacy draw.

Detection regret, by contrast, swings widely (₹122,731 → ₹216,845) because it
scales with the total value at stake, which scales directly with the recovery
probabilities the regimes rescale.

**Misdiagnosis costs most when actions are weakest.** Attribution regret is
*highest* under pessimistic efficacies (₹96,681) and lowest under optimistic
ones (₹85,750). When every action recovers well, picking the wrong one costs
comparatively little; when actions are marginal, picking the right one is what
carries the result. Attribution accuracy is identical (79.5%) in all three
regimes, as it must be — efficacies do not reach the diagnosis layer.

## 10. L3 — the pre-registered decision is NOT TO PROMOTE

§12.4: *"L3 enters the headline only if the paired improvement over B2 is
positive with a CI excluding zero AND materially reduces attribution regret."*
**L3 fails that test and is not promoted.** The rule was written before L3
existed and is applied as written.

### 10.1 The machinery works

Validated before trusting any negative result: on hand-built data with a planted
root (A→B lag 1, A→C lag 2, B→D lag 1), L3 recovers **ISSUER_A exactly** with the
correct parent structure. Whatever follows is a finding about the data, not a
bug in the implementation.

### 10.2 What L3 did on 78 dev scenarios

| | |
|---|---|
| eligibility rate | 100% |
| returned a root | 64% (50/78) |
| root accuracy vs ledger `primary_cell` | 34% [22%, 48%] |

Abstention reasons: 22 × "no edges survived FDR", 3 × "every node with outgoing
edges also has parents", 3 × "multiple roots with insufficient separation".

### 10.3 The null matters more than the number

**34% against a naive 1/8 = 12.5% chance looks like nearly 3× chance. That
comparison is wrong and would have produced a false headline.**

Identifying the affected *set* is L2's job. L3's only marginal claim is
precedence **within** that set. So the null L3 must beat is *pick a random
issuer from the affected set* — 1/k for k affected issuers.

| mechanism | affected issuers | L3 accuracy | chance |
|---|---|---|---|
| `issuer_degradation` | 1 | 69% | **100%** |
| `card_auth_spike` | 1 | 15% | **100%** |
| `psp_degradation` | 2 | 33% | **50%** |
| `upi_network_degradation` | 4 | 17% | **25%** |
| **pooled** | | **34%** | **70%** |

**L3 is significantly WORSE than chance** (one-sided binomial, p < 0.001). Its
root lands inside the affected set only **46%** of the time — more than half the
time it names an issuer the mechanism never touched.

For `issuer_degradation` and `card_auth_spike` exactly one issuer is affected, so
L2 already knows the answer with certainty and any L3 output can only degrade it.

### 10.4 Split by onset arm (§12.5, never pooled)

| arm | returned root | accuracy | chance |
|---|---|---|---|
| propagating | 54% | 30% | 70% |
| simultaneous | 82% | 39% | 70% |

**The propagating arm is where precedence was actually injected, and L3 does no
better there than on the arm with none.** That is the cleanest statement of the
negative result: L3 is not detecting the propagation that exists, it is
responding to noise in both arms alike.

This was foreseeable and was foreseen. Day 1 measured the injected onset lag as
recoverable at correlation **0.205** overall and **−0.008 below severity 1.5** —
the signal is weak where it exists and absent below mid severity. At T ≈ 288
windows with an incident occupying 4–36 of them, PCMCI is being asked to find a
structure that occupies at most 12% of the sample.

### 10.5 B3 − B2 is zero in value, by construction

§8.3 permits L3 to claim temporal precedence, not to reclassify the mechanism.
So L3 refines `cause_node`, while §9's policy reads only `mechanism_family` and
never `cause_node`. **B3 and B2 therefore execute identical actions and
B3 − B2 = 0 exactly — not because L3 underperformed, but because the spec gives
L3's output no path to an action.**

This was flagged on Day 0, before any code was written, and was never resolved
in the spec. Inventing an override rule on Day 7, after seeing L3 behave, would
be precisely the result-driven design the freeze exists to prevent. So L3 was
scored on its own terms instead, which is the stronger test anyway: it measures
L3's actual claim with no money in the way.

### 10.6 What this is worth saying

*At realistic observation density, statistical attribution plus routing topology
captured all the measurable diagnostic value, and conditional causal refinement
added none.* That is §1.1's second pre-registered outcome, stated in advance as
a publishable result rather than a failure.

The sharper version: **L3's output is worse than the prior L2 already supplies,
so a system that ran L3 and acted on it would be worse than one that abstained.**
That is an argument for the abstention machinery, not against it — and 36% of the
time L3 did abstain by name rather than guess.

## 11. The retry sequencer (28 Aug)

The measured 11.3% recovery rate was challenged as too low. Investigating it
changed what we built, and the write-up is separate because it is a different
kind of result: [`docs/RECOVERY_RATE.md`](docs/RECOVERY_RATE.md), with the
prior-art survey it rests on in [`docs/PRIOR_ART.md`](docs/PRIOR_ART.md).

In short:

- 11.3% divides by a denominator containing Rs 77,129 we deliberately declined
  to chase and 14 links no customer ever saw. It is not a system-performance
  figure.
- One recovery attempt caps you near the published no-automation band (20-31%).
  Every production dunning system runs a scheduled sequence instead. We now do
  too (`src/recovery/dunning.py`).
- The published 70-80% band is real and measures a **subscription** book. A
  model fitted only to the one-attempt band reproduces it on a subscription
  book (68.4%, in-band under plausible variation) and not on a checkout book
  (44.4%, out of band under every variation tested).
- The binding constraint is the **mandate**, not the message count. Removing it
  costs Book B 19 points.

## 12. A whole batch, end to end (29 Aug)

The brief asks to *"show measured money recovered across a batch, with
compliant escalation, stopping rules, and an audit trail."* The live account
gives us one measured recovery (Rs 28,433, §11) because we cannot make real
customers pay. So the batch is synthetic, and the boundary is drawn in exactly
one place and stated everywhere it matters.

**Two things are invented: which cases exist, and whether a contacted customer
pays.** Everything between them is the production engine, unmodified -- the
schedules from `config/declines.yaml`, the contact decision from
`dunning.authorize_contact`, quiet hours, contact ceilings, opt-out checks,
promise pauses, per-run caps, the kill switch, the append-only ledger, and
`campaign.reconcile_links` for measurement. `src/sim/` re-implements no rule
and is not permitted to skip one: an opt-out is written to the suppression file
and then *discovered* by the engine on a later pass, exactly as a real one is.

    python -m scripts.simulate_batch          # 240 cases, 21 days, ~2 min

### 12.1 The result

```
240 cases            Rs 31,94,803 at risk    (48 invoices, 108 failures, 84 carts)
 25 not chased          Rs 16,165            below the per-workflow exposure floor
215 chased           Rs 31,78,638

recovered            Rs 10,66,105            33.5% of revenue chased
                     74 of 215 cases         34.4%  [95% CI 28.4% - 41.0%]

516 contacts         Rs 29 cash              414 e-mails @2p, 102 SMS @20p
                     Rs 42,650               the price of 516 interruptions
net contribution     Rs 10,23,426            25.0x the cost of working the book
```

Both cost lines changed once delivery was wired, and both were wrong before in
the same direction. Cash was charged per ladder STEP at a flat 20p, because
there was no delivery channel to charge for -- every notification row said
"none". It is now counted per MESSAGE from the notification ledger, and an
e-mail is not an SMS. And "contacts" counted `attempt_succeeded` as well as
`attempt_delivered`, so every recovered case was billed for one interruption it
never received; the gap was exactly the 74 recoveries. A reconcile discovering
that a link was paid is us reading the gateway, not a second message to the
customer.

Per stream, because one aggregate hides three different problems:

| stream | chased | recovered | rate |
|---|---|---|---|
| payment failures | Rs 6,66,350 | Rs 2,63,195 | 39.5% |
| overdue receivables | Rs 20,65,827 | Rs 7,92,966 | 38.4% |
| checkout abandonment | Rs 4,46,461 | Rs 9,944 | **2.2%** |

That last row is the one worth reading. Abandonment is 14% of the money at
risk and 0.9% of the money recovered, and it lands below the 3-8% band
docs/RECOVERY_RATE.md collects for abandoned-cart recovery. It is the stream
where an optimistic assumption would be least visible and least defensible, so
it carries the least generous ceiling in the model.

### 12.2 What this number is, and is not

**It is not a measurement.** The rate is a consequence of the per-contact
conversion assumption in `src/sim/customer.py`. That assumption is not invented
for the batch -- it is the single free parameter `scripts/recovery_curve.py`
fits against a published third-party one-attempt band, reused rather than
re-chosen so the two analyses cannot drift apart. But a fitted assumption
propagated through real machinery is still an assumption. Change it and a
different number comes out; that is what an assumption is.

**The only measured recovery in this project remains the Rs 28,433** on the
live Razorpay test account, read back from the API.

### 12.3 What the batch does establish

Everything the conversion assumption does not touch -- and this is the part a
single live case cannot show at all. Each is checked against the ledger by
`scripts/simulate_batch.py` and printed as PASS/FAIL, not asserted in prose:

```
[PASS]  no contact after an opt-out        23 opted out, 0 messages to any of them after
[PASS]  no message outside permitted hours every message inside its own channel's window
[PASS]  no case over its contact ceiling   cap is 3 per case; worst in the batch is 3
[PASS]  every attempt has an outcome       516 written ahead, 0 without a recorded outcome
[PASS]  no recovery counted twice          74 recovered, none counted twice
```

Stopping rules, one row per sequence:

```
terminal_channel_reached   110    Rs 17,14,706   ladder exhausted, handed to a human
recovered                   74    Rs 10,66,105
customer_opted_out          18       Rs 93,888
contact_ceiling_reached     12     Rs 3,00,970
```

And the escalation actually escalates: 449 contacts held by quiet hours rather
than dropped, 73 steps planned but not performed (no mandate, no telephony
provider), 9 promises to pay recorded and honoured as pauses (3 kept, 6 broken
and resumed), 5 passes halted by a per-run cap, 3,025 append-only ledger events
across 215 campaigns.

**Twelve cases, Rs 96,198, paid a link after their ladder had already
stopped.** Reconcile runs over the ledger rather than over live sequences, so
that money is still found. Chasing had stopped; measuring had not. An engine
that refused those payments because the sequence was closed would have lost
9.0% of everything this batch recovered.

(That figure was first written here as "Rs 1.6 lakh", estimated from the case
count rather than computed. It is 96,198. The report now prints the amount so
the next person does not have to estimate it either.)

### 12.4 What building it found

Four defects, none of which the live book was large enough to expose:

- **`reconcile_links` bypassed the gateway abstraction.** The one function that
  decides what counts as recovered revenue called Razorpay directly, while
  `fetch_recovery` on the adapter had been doing exactly that call all along.
  Now routed through the gateway, so any provider is measured by identical code.
- **A stranded `return` in `campaign.py`.** `execute_step`'s fallback for an
  unknown channel sat after `_customer_of` had already returned, referencing a
  variable not in its scope. Dead code, so the function fell off the end and
  returned `None`. Latent rather than live -- `terminal_channels` catches the
  only channels that would reach it -- but it was the difference between adding
  a channel and getting a clear answer, and adding one and getting an
  `AttributeError`.
- **Every config loader re-parsed its YAML on every call.** `may_open` consults
  the per-workflow floor for every item on every pass, so a 50-case batch spent
  91 of 105 seconds in the YAML parser. Now cached on path+mtime+size -- not
  `lru_cache`, which would have silently disabled the global kill switch that
  `policy.yaml` exists to carry. `tests/test_yamlcache.py` pins that.
- **The simulator could corrupt its own results.** It wrote to fixed paths with
  no lock while `fresh()` truncated them, so two overlapping batches deleted
  the file each other was reading. That is not hypothetical; it is how the
  first full-size run ended. It now takes the same run lock the live runner
  takes.

## 13. The two loops that could not run (1 Sep)

Payment degradation and subscription failure were both declared with full
ladders and neither could execute a single step. The blockers were different
and neither was a missing feature:

- **Degradation was starved.** The CUSUM needs ~30 attempts in a segment to
  separate a rate shift from noise. The live account's busiest cell has one.
- **Subscriptions is gated.** `/subscriptions` and `/plans` return 401 on a key
  that reads `/payments` and `/customers` fine. Re-probed 1 Sep: still 401.
  That is a product entitlement, not a credentials problem, and no code fixes
  it.

Both blockers remain true and both are still reported. What changed is that the
stretch of code between "we found something" and "we did something about it" is
no longer unreachable — and **three real bugs were living in it**.

### 13.1 What was hiding there

- **The attribution ladder crashed on every real issuer.**
  `evidence.candidate_nodes` indexed `routing[issuer]` directly, and
  `config/topology.yaml` holds the abstract `ISSUER_A..H` of the frozen
  experiment while Razorpay reports HDFC, SBIN and the rest. The degradation
  loop was guaranteed to `KeyError` the first time a real book gave it enough
  volume to reach diagnosis. It only ever escaped that because the live account
  is too quiet to detect anything. An issuer with unknown routing now
  contributes no PSP or network candidate — which is what §8.2 requires anyway,
  since positing a latent node is forbidden — but still contributes itself.

- **The mechanism was diagnosed from the background, not the incident.**
  `dominant_source` was the mode over *every* failed payment on the account. On
  a book failing at a healthy 9% with one cell blown out to 62%, ordinary
  customer declines outnumber the incident's failures about eight to one, so
  the source came back `customer`, no mechanism family matched, and the loop
  chose `NO_ACTION`. Detection worked, localisation worked, and nothing
  happened — the most expensive kind of quiet failure, because every visible
  intermediate step looked right.

- **Subscriptions were routed to the wrong ladder.** `workflows.yaml` declared
  `schedule: mandate` and nothing read it: `classify_item` fell through to the
  reason-based classifier, so a failed recurring charge went onto the 5-step
  `slow` checkout ladder and all four silent rungs the workflow exists for were
  absent from the plan. `rebuild` had the same gap, which additionally broke
  its own documented guarantee that replanning reproduces the same steps.

A fourth was introduced and caught while building: enabling a mandate on the
simulated gateway handed a free silent retry to *every* one-off payment
failure, because `fast` and `slow` each open with a `requires_mandate` rung too
— not just the 7-step `mandate` schedule. A mandate is a property of the
**case**, not of the provider; a merchant can have Subscriptions enabled and
still take one-off payments with no stored instrument. Both conditions are now
required, and `tests/test_sim.py` pins it.

### 13.2 Degradation, end to end

`python -m scripts.ops degradation` — simulated traffic, production detector.
The scan is not told which segment is degrading, when, or that anything is
wrong at all.

```
INJECTED    SBIN/netbanking failing at 62% for 45 min   (never shown to the scan)
ALERTING    SBIN/netbanking  36/170 = 21%
cause       issuer:SBIN|netbanking
mechanism   issuer_degradation
confidence  0.83  (L2)
ACTION      ALTERNATE_METHOD_LINK
```

One alerting cell, no false positives, and the answer is a reroute rather than
a dunning message — an incident is fixed by moving traffic, not by chasing the
customers who happened to be caught in it. `--live` runs the identical loop
against the real account and returns the honest verdict: 6 payments, thin,
nothing to fix.

### 13.3 What a mandate is worth

With the subscription stream in the book, all four loops run:

| stream | recovery rate, one draw |
|---|---|
| subscription failure | **70.4%** |
| overdue receivables | 53.4% |
| payment failures | 52.8% |
| checkout abandonment | 22.8% |

The subscription number lands inside the published 70–80% band, and
docs/RECOVERY_RATE.md predicted 68.4% for a mandate-backed book from a
parameter fitted only to the one-attempt band. The batch and the analysis agree
without the batch being tuned to it.

### 13.4 One seed is not a result

The table above is a single draw and the abandonment row is the top of its
range. Across four seeds:

```
             abandon   failure   overdue    subscr
range        5.6-23.5% 33.7-56.4% 60.0-67.6% 51.7-69.6%
```

Fifty abandoned cases against a 22% recoverability ceiling means the recovered
count is a handful either way, and the spread is wider than the binomial
interval from any one run because the persona draw varies too. The default seed
sits at the top for abandonment; the other three (5.6%, 7.5%, 11.8%) sit in the
published 3–8% typical / 10–14% leaders bands. `--seeds 4` prints the spread,
and the per-run report now says so rather than letting one draw read as a
measurement.

## 14. Still to come

- L3, B3, the L3 headline decision, and the §12.5 split by onset arm.
- Split/merge counts, OOD recognition rate, accuracy at coverage per class, and
  the explicit failure list.
