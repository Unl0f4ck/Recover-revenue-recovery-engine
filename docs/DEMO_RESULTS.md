# Verified demonstration — 5 September 2026

## Synthetic batch

Run ID: `e1baf8457f9e4d9092c0385b7331e23f`.
Configuration: 240 generated cases, 28 days, seed 20260905.

| Measure | Observed result |
| --- | ---: |
| Generated cases | 240 |
| Below the configured exposure floor | 40 |
| Opened campaigns | 200 |
| Recovered cases | 77 |
| Recovered / opened campaigns | 38.5% |
| Opened cohort exposure | INR 2,899,298 |
| Reconciled simulated recovery | INR 1,611,978 |
| Still open at the end | 0 |
| Contact attempts | 451 |
| Total write-ahead attempts | 504 |

All five ledger checks passed: no contact after opt-out (15 opt-outs), permitted
hours, per-case contact ceilings (maximum 3), an outcome for every write-ahead
attempt, and one recovery per reference (77 recovered references).

These are **simulated** customer outcomes, not real commercial collections or
proof of incremental uplift. The denominator is the 200 opened campaigns, not
the 240 generated cases. The batch engine determines which cases clear the
exposure floor and which later stop or recover.

The portable bundle at `artifacts/recover-demo-evidence.zip` includes metadata,
the campaign/notification/promise/suppression ledgers, labeled metrics and a
SHA-256 checksum manifest. All archive member hashes were verified after export.
The bundle is ignored by Git and can be regenerated using `scripts.export_run`.

## Live integration checks

- The running HTTP service read the existing Razorpay test account and returned
  an isolated preview: 35 campaigns opened, 2 preview actions. The account held
  6 payments across 5 segment/method cells, too little traffic for an outage
  conclusion. No gateway write or customer notification was requested.
- Gemini interpreted the synthetic reply “Friday tak payment kar dunga, please
  wait.” as `promise_to_pay`, with September 11 as the date relative to September
  5, and reported confidence 0.95. This was one live smoke check, not a measured
  language-model accuracy evaluation.
- Full suite: 412 tests passed in the final full run. A subsequent customer-link
  ownership/closed-campaign regression was added; all 21 focused integration
  tests passed after that change.
- JavaScript syntax, referenced DOM IDs, package installation, HTTP callback
  flows and evidence archive checks passed.
- Browser automation was unavailable in the development session. Visual and
  responsive browser QA is still needed before recording the submission video.

## Account connection and messaging extension

- Verified the running HTTP connection endpoint against the existing Razorpay
  test credentials: connection, account-scoped status and disconnect all returned
  HTTP 200. The check used a separate client session and only a provider GET.
- Added Connections and Messages screens, account-scoped files and callbacks,
  explicit email/SMS controls, and journalled one-time sends for existing links.
- The focused 42-check integration run passed. It includes independent accounts,
  rejected live keys, redacted validation errors, expired connections, scoped
  webhooks, known-recipient SMS/email routing, quiet hours, opt-outs, promises,
  duplicate/uncertain sends and a manual-send-to-automation timing guard.
- Final full suite after the extension: **434 passed in 83.22 seconds**.
- Notification provider requests were mocked; no real email or SMS delivery was
  tested. The updated HTML and JavaScript assets return HTTP 200, and both scripts
  pass Node syntax checks. Browser automation remains unavailable.

## What has not been claimed

No production payment, new real-world customer contact, production deployment,
public repository publication, application submission, comparative uplift, or
competition result was performed or established by these checks.
