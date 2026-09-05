# Recover — Revenue Recovery Engine

Diagnosis-aware, bounded recovery for Razorpay test accounts. Detect unpaid
revenue, choose a recovery path, request SMS/email, interpret customer replies,
and count recovery only after a gateway outcome confirms payment.

This is my submission for [Razorpay AI Buildathon — Track 03](https://razorpay.com/buildathon/).
Everything below is written for whoever is reviewing it: how to run the system,
what to look at, and how to check that the claims on this page are true. Every
figure the console shows is reproducible from this repository, and the sections
on limits state plainly what the system does *not* establish.

![Recover control room with explicitly simulated batch results](docs/images/control-room.png)

## Run it

Requires Python 3.11+.

```bash
python -m pip install -e '.[dev]'
python -m scripts.serve
```

Open **http://127.0.0.1:8000**. No API keys are needed for the batch lab — the
first four checks below run on synthetic data alone.

## Check that it is correct

Start with the automated suite. It runs from a clean clone with no keys, no
network and no account data:

```bash
python -m pytest -q
node --check ui/control/app.js
node --check ui/control/connections.js
```

The same three commands run on every push via GitHub Actions
(`.github/workflows/test.yml`), so the badge history shows them passing on a
clean Ubuntu runner rather than only on my machine. Tests use synthetic data
and mocked outbound provider calls; none of them contact Razorpay.

Then walk the console. In order, these check the claims made under *What works*:

1. **Diagnosis.** Run the 240-case synthetic demo and inspect the full cohort.
   Open any case for its diagnosis, schedule, controls and append-only audit.
2. **Data hygiene.** Import `samples/review-demo.csv` — two valid rows, one
   duplicate, one invalid amount. Confirm the duplicate and the bad amount are
   both caught rather than processed.
3. **Customer controls.** In that import, open `order_review_demo`, read the
   `STOP` reply, and apply the opt-out. Confirm the contact is suppressed.
4. **Simulation labelling.** Check that every synthetic figure is marked as
   simulated in the UI. Nothing on the synthetic path should read as real money.
5. **Account isolation** *(needs your own Razorpay **test** keys)*. Open
   **Connections** and verify them. Live keys are rejected outright; account
   records are separated by verified key identity.
6. **Bounded execution.** Preview the account, then explicitly create a bounded
   batch of test links. Enable SMS/email only for recipients you have permission
   to contact. Nothing sends without that explicit step.
7. **Honest accounting.** Inspect **Messages** and export the evidence bundle.
   Confirm the distinction holds throughout: requested is not delivered, and a
   created link is not recovered revenue.

## What works

- Decline classification, statistical degradation diagnosis, and persisted
  per-case schedules across failed payments, abandoned checkouts and receivables.
- Fresh obligation-state and balance checks; duplicate-obligation suppression.
- Just-in-time replacement-link cancellation after policy and budget checks.
- Write-ahead attempts, stable operation keys, uncertain-write reconciliation.
- Customer opt-out, quiet hours, contact caps, promises and human escalation.
- Optional Gemini reply interpretation with deterministic validation and a safe
  unavailable-model fallback; English, Hindi and Hinglish reply support.
- Razorpay-native SMS/email requests, one-time existing-link notifications,
  account-scoped signed webhooks, and separate notification audit states.
- Reproducible synthetic batch evidence and a tested FastAPI/vanilla-JS console.

## What it does not claim

I would rather state these than have a reviewer find them:

- Synthetic recovery totals measure engine behaviour under a stated customer
  model. They are not production cash and not causal uplift.
- The Razorpay adapter cannot charge saved mandates; that path is simulated.
- Payment-method exclusions are intent, not a verified enforcement claim.
- Native notification delivery has not been verified at a real inbox or handset.
- Connections keep secrets in server memory for eight hours; reconnect after a
  restart.
- This is a **single-process prototype**, not a hardened multi-tenant SaaS.
  Remote use requires HTTPS and an operator token.
- Customer self-service token routes currently belong to the server workspace,
  and native notification templates do not automatically carry an unsubscribe URL.

## Evidence

[RESULTS.md](RESULTS.md) reports the frozen-set evaluation — 240 scenarios
generated *after* the pre-registration tag, with confidence intervals, the
negative-class defects and the sensitivity sweep. [PREREGISTRATION.md](PREREGISTRATION.md)
is what makes that checkable: it fixes the configs and code hashes *before* the
frozen set existed, so nothing reported was tuned against it. The frozen
evaluation data behind both is in `data/frozen/`.

Where the headline recovery rate comes from, and what a higher one would
require, is worked through in [docs/RECOVERY_RATE.md](docs/RECOVERY_RATE.md),
resting on the survey of eight production systems in
[docs/PRIOR_ART.md](docs/PRIOR_ART.md).

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — how the pieces fit together
- [Account connection and messaging](docs/CONNECTIONS.md) — connecting a Razorpay account and configuring delivery
- [Operations](docs/OPERATIONS.md) — running batches, reconciliation and day-to-day operation

This is a source copy. It excludes my original Git history, `.env`, private
account ledgers, operational databases and generated customer data; everything
needed to install, run and verify the system is here.
