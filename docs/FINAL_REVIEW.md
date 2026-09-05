# Final system review — 5 September 2026

## Verdict

Recover is a credible Track 03 buildathon prototype, not a guaranteed selection
and not a production payments service. There is no defensible percentage for
selection without applicant-pool, judging and eligibility data. Passing tests
is evidence of engineering quality, not a selection probability.

The official [Revenue Recovery track](https://razorpay.com/buildathon/) asks for
detection, intervention and bounded execution, with measured recovered value
across a batch, escalation, stopping rules and an audit trail. It is a student
internship program; the applicant must check eligibility and availability.

## Audit method

Story: connect a test account → ingest unpaid obligations → authorize recovery →
request a notification → reconcile payment → stop or escalate → export evidence.
The full-story verification workflow guided boundary checks and regression fixes.
External notification calls were mocked; no test asserted real delivery without
provider evidence. Existing credentials were used only for read-only smoke checks.

## Broken paths fixed

| Issue | Fix / regression evidence |
| --- | --- |
| Old link cancelled before a replacement passed guards or budget | Cancellation moved to the already-authorized create boundary; quiet hours, opt-outs, kill switch and zero-budget passes leave links untouched. |
| Unknown gateway status permitted new actions | Unknown, partial or unsupported states hold the case. |
| Webhook-closed cases left payable links behind | Later reconciliation sweeps retire their outstanding links, including cases absent from the feed. |
| Intervention cap stopped later paid cases from closing | Stopping is evaluated independently of intervention budgets. |
| Partial/edited balances could produce stale-value recovery links | Fresh outstanding amount is compared with the campaign; mismatches require review. |
| A failed payment duplicated an invoice/link/order obligation | Backing-order identities are resolved within the connected account and deduplicated across streams. Mapping failures fail closed. |
| Abandoned Payment Links lost known customer contact | Ingestion preserves provider email/phone identity for suppression and delivery. |
| CSV silently rounded money or accepted duplicate/invalid rows | Decimal whole-paise validation, supported-stream validation and explicit rejected-row reasons. |
| Two reply confirmations could apply the same proposal twice | Cached result is rechecked and stored within the mutation lock. |
| STOP was recorded but the UI still showed active campaigns | Matching customer campaigns immediately receive opt-out stop events. |
| Startup selected an arbitrary old run | Completed runs are ordered by creation timestamp. |
| Provider response missing a usable link looked successful | Missing ID/HTTPS URL is held as an uncertain operation, not recorded as a usable link. |
| Example webhook secret advertised as a configured capability | Placeholder signing secrets are rejected/disabled rather than enabling a predictable public callback. |
| Local tests relied on real account credentials | Subscription entitlement/fallback tests now inject provider responses; the public release can test without `.env`. |
| Quick demo could display a different form seed than the submitted job | Progress rendering synchronizes the form with the job's actual configuration. |
| Clean CI could not import the statistical diagnosis engine | Tigramite is now an explicit, version-pinned runtime dependency rather than relying on a developer's installed environment. |

## Verification evidence

- Full workspace regression run: **446 passed** in 88.69 seconds; subsequent
  placeholder-secret regression was also added and passed in the focused run.
- Clean, credential-free release validation: **441 passed, 6 skipped** in 87.15
  seconds. The skipped checks depend on intentionally excluded local redirect
  contacts or a locally seeded subscription book; they are not core HTTP or
  payment safety checks. No production credentials are needed by CI.
- Targeted account, ingestion, notification, callback and final-audit regression
  tests pass. Test coverage includes opt-outs, uncertain writes, one-time sends,
  changed balances, account isolation and original-obligation settlement.
- Browser automation is now available using agent-browser. Control room,
  Connections, Messages, CSV import results and case inspection were exercised.
  No JavaScript runtime errors were reported in the inspected flows. CSV browser
  submission used `requestSubmit()` after click automation did not dispatch the
  form; physical mouse-submit behavior is not claimed from that check.
- The browser STOP flow was exercised using the Read reply and Apply reading
  buttons: `order_review_demo` transitioned from WAITING to OPTED_OUT. Desktop
  and 390px mobile screenshots were inspected; tables/navigation scroll on
  narrow screens. [Control room](images/control-room.png),
  [Connections](images/connections.png), [mobile](images/mobile.png).
- Credential-free synthetic batch runs remain reproducible by explicit seed.
  Historical demo totals belong to their recorded configuration; they must not
  be presented as live revenue or as the output of a different seed.

## Submission assessment

| Requirement | Assessment |
| --- | --- |
| Detect revenue at risk | Implemented, with source identity and stream-specific schedules. |
| Choose an intervention | Implemented using decline classes and diagnosis; explainable audit evidence. |
| Execute within limits | Implemented in test mode, with holds, contact budgets and notification controls. |
| Reconcile and stop | Implemented via signed webhooks and polling, with regression coverage. |
| Batch recovery evidence | Synthetic engine evidence is available; not proven commercial uplift. A recorded provider-backed test cohort would strengthen the submission. |
| Meaningful AI | Gemini-assisted reply interpretation and statistical diagnosis. Mandate execution and behavioral recovery remain simulated. |
| Public repo and architecture | Published and public-access verified at [Unl0f4ck/recover-revenue-recovery](https://github.com/Unl0f4ck/recover-revenue-recovery). Private runtime data and original local history were excluded. |
| Five-minute pitch and applicant eligibility | Must be completed/confirmed by the applicant. |

## Remaining limitations — do not hide these

1. No guaranteed selection, measured commercial uplift or independent customer
   pilot. A judged build needs evidence and an explanation, not just feature count.
2. No demonstrated real inbox/handset delivery in this review. Choose consenting
   test recipients and record the result separately before claiming it works live.
3. Single-process, file-backed prototype. Sessions expire after eight hours and
   lose credentials on restart. Hosted production needs durable secret storage,
   real operator identities, transactional storage, retention rules and monitoring.
4. Native notification templates are provider-controlled. Customer token routes
   are server-workspace features, not automatic connected-account unsubscribe
   links. Do not claim full messaging compliance or inbound WhatsApp/voice support.
5. Saved-mandate charging and verified payment-method enforcement are unavailable
   in the current real adapter. Their simulation is explicitly labelled.
6. The public repository, video and application are distinct deliverables. The
   applicant should demonstrate a recovered case and a deliberately blocked case,
   explain one tradeoff, and keep simulation labels visible throughout.

Best next evidence: a small consented Razorpay test-mode cohort with captured
provider events, a before/after ledger, notification results, and the applicant's
own five-minute explanation. This improves the submission; it cannot make judging
certain.
