# Recover — Track 03 submission

## Product thesis

**Recover turns unpaid revenue into an explainable, bounded recovery campaign,
and only counts money after the gateway confirms payment.**

The strongest demonstration is a complete batch and a difficult customer case:
show what was recovered, show what was deliberately left alone, and show why.
Do not claim a guaranteed recovery rate, production cash movement, or an
unmeasured uplift over another vendor.

The [official track brief](https://razorpay.com/buildathon/) asks for detection,
intervention and bounded execution across revenue-loss workflows. It explicitly
requires measured batch recovery, compliant escalation, stopping rules and an
audit trail. Submission materials include a public repository, architecture and
a five-minute pitch video. Check the current application form before submitting.

## Five-minute demonstration

| Time | What to show | Point to make |
| --- | --- | --- |
| 0:00–0:35 | Control room and four revenue streams | A failed checkout and an overdue invoice need different treatment. |
| 0:35–1:20 | Run a 240-case batch or open the completed seed 20260905 run | The engine works across a cohort, with a visible denominator and simulation label. |
| 1:20–2:05 | Open a recovered invoice; inspect attempt, payment and stop events | A created link is not a recovery. Reconciliation is what closes the loop. |
| 2:05–2:55 | Import `samples/failures.csv`; open a case; read a Hinglish reply | Gemini interprets language. The deterministic layer validates dates and intent. |
| 2:55–3:35 | Preview and apply `STOP`; show the recorded action | Explicit opt-out works even when AI is unavailable. An unreadable/disputed reply gets a human handoff. |
| 3:35–4:15 | Evidence tab and exported ledger | Quiet hours, ceilings, write-ahead outcomes and duplicate accounting are checked against records. |
| 4:15–4:45 | Razorpay test ledger and account preview | Distinguish provider-confirmed test payments from simulated behavioral outcomes. |
| 4:45–5:00 | Architecture and next deployment boundary | A single-writer test-mode prototype; production requires provider entitlement and durable operational infrastructure. |

Run the batch before recording so the video can show the complete result without
waiting. Keep the seed and horizon on screen. If the AI provider is unavailable,
show its review fallback rather than editing a canned answer into the recording.

## Demonstrable engineering depth

- Derived operation IDs plus a durable create journal; an uncertain POST is
  reconciled, with no blind retries.
- Original-obligation verification handles payment elsewhere, beyond recovery
  links. Order webhooks preserve order identity even when a payment entity comes
  first in the payload.
- Persistent campaign plans keep policy changes from rewriting prior attempts.
- Human-readable audit timelines and downloadable evidence behind every number.
- Clear separation of imported previews, behavioral simulation, and Razorpay
  test-mode outcomes. No claimed production revenue.
- An attribution research layer with a pre-registered rejection of an ineffective
  L3 model. Its failure is part of the evidence, not something to conceal.

## Before publishing

For the reviewed application, use the clean source export produced by
`python -m scripts.public_release artifacts/<fresh-release-directory>`.
It uses an explicit file list, scans active credential values, excludes private
runtime books and original Git history, and uses the current public README.
Publishing is a separate explicit `--publish` operation; see `--help`.

1. Run `python -m pytest -q` and the demo batch on the final source revision.
2. Record exact run configuration, generated cases, opened cases, paid cases,
   recovered amount and invariant results. Use the run's ledger, not copied totals.
3. Review tracked `data/live/` and `ui/data.js`: these predate this update and may
   contain customer contacts, provider URLs or financial test records. Publish a
   sanitized fixture instead of exposing personal/account information.
4. Keep `.env`, `config/redirect.local.yaml`, operational databases and generated
   `data/console/` records private. `.env.example` contains names only.
5. Record your own narration explaining a design tradeoff and a handled failure.
6. Publish the repository and video, verify the links from an unauthenticated
   browser, then complete the application yourself. Building does not submit it.

## Honest limitations

- Razorpay adapters refuse production keys. Existing recorded test outcomes are
  not evidence of real commercial recovery.
- The current adapter cannot charge stored mandates; that path runs against the
  simulated gateway. Do not imply an account toggle implements the missing API.
- Alternate-method intent is recorded on Payment Links. This account's method
  enforcement has not been verified. The separate Checkout demo is illustrative.
- A statistical diagnosis changes the technical-failure campaign to an early
  alternate-method step. Automatic merchant PSP configuration changes are not
  implemented and are not claimed.
- Simulated recovery is conditional on the customer model; it is not causal
  proof of incremental revenue.
