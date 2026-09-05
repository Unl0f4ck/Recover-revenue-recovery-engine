# Feature gaps against the eight reference systems

A second pass over the repositories in [`PRIOR_ART.md`](PRIOR_ART.md), read with
a different question. The first pass asked *what shape is a dunning system*, and
the answer rebuilt the product. This one asks the narrower question:

> **What do they have that we don't, and which of it is worth having?**

Four items turned out not to be features at all. They were controls we had
already declared — in config, in the code, in the safety section of the
README — that **nothing on the sequencer path ever read**. Those are fixed. The
rest is a ranked backlog.

---

## Part 1 — dead controls, found and fixed

A safety control that is documented, plumbed through every function signature,
and never actually read is worse than one that was never written. Everyone
downstream believes it works. All four of these predate this review.

### 1.1 The kill switch did not work

`bounds.global_kill_switch` has been in `policy.yaml` since day one. It was
accepted as a parameter by `run_pass`, `advance` and `check_stop` — and **no
caller ever loaded it from config**, so the production path defaulted it to
`False` forever. Only the old one-shot `runner.py` read it.

recoup makes this first-class: a flagged merchant "cannot emit charges across
all code paths". Ours now halts the run before a single sequence is opened, and
reports why.

*Fixed. `tests/test_controls.py::test_kill_switch_is_read_from_config_not_just_accepted`*

### 1.2 The per-run ceilings did not apply to the sequencer

`batch_limits.max_interventions_per_run` and `max_exposure_per_run_paise` were
read only by `src/recovery/runner.py` — the pre-sequencer batch path. The
sequencer, which is the path that now matters, **ran uncapped**. Etherlabs
carries a budget on every decision record for precisely this reason: the point
of a batch limit is that one runaway pass cannot touch the whole book.

Now enforced in `run_pass`, which stops on the attempt that crosses either cap
and records `halted` with the numbers.

*Fixed. Two tests.*

### 1.3 There was no opt-out

**The largest gap, and it was a compliance gap rather than a missing feature.**

recoup sends "signed card-update and **opt-out** links" on every message. We had
nothing. A system that contacts a person up to three times per unit of at-risk
revenue, across an unbounded number of units, with no way for them to say stop,
is not compliant in any sense the track brief means — however carefully its
quiet hours are configured.

New `src/recovery/suppression.py`. Two deliberate properties:

- **Absolute.** Checked *above* quiet hours and the contact ceiling, because
  those answer *when* we may contact someone and this answers *whether*. A
  suppressed contact is refused, not deferred.
- **Append-only**, like the ledger. Un-suppressing is itself an event and
  requires a reason and a named source, so "who put this person back on the
  list" is answerable.

Identity is normalised (case, whitespace, phone formatting) — suppression that
misses because of a trailing space is suppression that does not exist.

`python -m scripts.run_dunning --opt-out a@b.com --reason "replied STOP"`

An opt-out gets its own stop reason, `customer_opted_out`, rather than being
collapsed into `contact_ceiling_reached`. "We have messaged this person enough"
and "this person asked us to stop" are different facts, and the second is the
one an auditor cares about.

*Fixed. Five tests.*

### 1.4 Two concurrent passes would both send

recoup and Etherlabs independently use `FOR UPDATE SKIP LOCKED` retry leases.
The property they buy is that two workers cannot pick up the same attempt.

We store state in JSONL, so there is no row to lock — and the exposure is worse:
`run_dunning --execute` twice at once, or a cron firing while a human runs it by
hand, and both processes see the same step due, both write ahead, and both
create a link. **The idempotency key protects the provider from a double charge;
nothing protected the customer from two messages.**

New `src/recovery/runlock.py` — an `O_CREAT|O_EXCL` lock held for the length of
a pass. Deliberately unforgiving: a second runner refuses and names the holder
rather than queueing, because a pass that waits its turn and then fires the
message the first one already sent is the bug, not the fix. Stale locks are
reclaimed by age, not by a pid check (wrong across containers).

*Fixed. Three tests.*

---

## Part 1b — everything in the backlog, built

Both tiers were subsequently implemented. What follows is the original ranking,
with what each turned into and what building it exposed.

| # | feature | built as | tests |
|---|---|---|---|
| 1 | webhook ingestion + HMAC | `src/recovery/webhooks.py`, `src/gateways/razorpay_gateway.py` | 10 |
| 2 | manual-review queue | `src/recovery/review.py`, console screen 4 | 5 |
| 3 | actually deliver the message | `create_recovery_link(notify=…)` | — |
| 4 | notification ledger | `src/recovery/notify.py` | 6 |
| 5 | daily reconciliation report | `reports.daily`, `reports.find_drift` | 2 |
| 6 | CSV ingest | `src/ingest/csv_source.py` | 4 |
| 7 | payment-update token | `src/recovery/tokens.py` | 7 |
| 8 | analytics over time | `reports.by_decline_class` / `by_attempt` / `over_time` | 2 |
| 9 | per-merchant configuration | `src/recovery/merchants.py`, `config/merchants.yaml` | 4 |
| 10 | gateway adapter interface | `src/gateways/` | 2 |

### Five more defects, found by building them

**Duplicate suppression could stall a campaign permanently.** The check asked
"has a message for this rung already been requested" with no time bound. The
window it protects is narrow -- we recorded a request, then died before
recording the outcome -- and once an outcome IS recorded the attempt counter
advances and the check cannot fire anyway. So unbounded it protected nothing
extra and froze a sequence forever after one crash. It froze the 14-day
projection on day zero. Now bounded to the minimum gap between attempts.

**The projection wrote to the live notification ledger.** Every build left
contact records that the NEXT build read as real history, so every rung looked
already-sent. The projection now has its own scratch ledgers.

**A missing customer identity disabled the whole contact gate.** The first
suppression cut returned early when `customer_ref` was None -- correct for the
opt-out check, catastrophic for everything after it, since a Razorpay *order*
carries neither email nor phone. Every abandoned checkout would have bypassed
quiet hours.

**The per-customer daily cap counted status rows, not messages.** One message
accumulates several rows as it moves requested → sent → delivered, so a cap of
two blocked after the first message.

**Ledger reads were quadratic.** Every predicate re-read and re-scanned the
whole file, and the 14-day projection asks several questions per sequence per
pass. The console build stopped completing. Fixed with a parse cache keyed on
(path, mtime, size) and a per-reference index — 86s to 18s.

Two of my own tests were also testing nothing: they used a schedule opening on
a *silent* rung, which is unexecutable on this account, so they advanced the
ladder while contacting nobody and spending nothing.

## Part 2 — the original ranked backlog

Ordered by value to *this* project: a Razorpay submission judged on measured
money recovered, compliant escalation, stopping rules and an audit trail.

### Tier 1 — build next

| # | feature | from | why |
|---|---|---|---|
| 1 | **Webhook ingestion with HMAC verification** | Etherlabs, ajithmanmu | We *poll*. Razorpay emits `payment.failed`, `payment_link.paid`, `order.paid`. Webhooks make detection near-real-time and reconciliation event-driven instead of a cron. Etherlabs verifies against **exact raw bytes**, supports multiple `v1` signatures and a timestamp tolerance — all three matter and all three are easy to get wrong. The honest 80% is buildable and testable offline: the verifier plus the handler, without a public endpoint. |
| 2 | **Manual-review queue** | Etherlabs, Kill Bill | Our `human_review` channel escalates and stops — and then nothing. There is no queue an operator can open, no way to act on one, no ageing. The console shows an ESCALATED count and no list. Escalation that goes nowhere is a stopping rule wearing a handoff's clothes. |
| 3 | **Actually deliver the message** | dunlo, emp-billing, recoup | We create Payment Links with `notify: {sms: false, email: false}`. Nothing is ever sent to anyone. That is honest for a test account, but it means "contact made" really means "link created", and the compliance machinery is guarding a door nobody walks through. Razorpay Payment Links can notify natively — turning it on (with the opt-out now in place) would make the whole escalation ladder real. |
| 4 | **Notification ledger with delivery status** | Etherlabs, emp-billing | Follows directly from 3. emp-billing tracks `webhook_deliveries` and `notifications` with delivery state; Etherlabs has a notifications ledger and duplicate suppression. Sent ≠ delivered ≠ opened, and a recovery system that cannot tell them apart cannot explain a bad campaign. |
| 5 | **Scheduled daily reconciliation + report** | recoup | recoup runs a "daily job… cross-checking books against gateway records". We reconcile on demand. A dated report — recovered since yesterday, still open, newly escalated — is cheap, and it is the artefact an operator would actually read. |

### Tier 2 — worth building, more work

| # | feature | from | note |
|---|---|---|---|
| 6 | **CSV ingest** | recoup | Lets a merchant run the engine over their own exported failures with no API access. Strong demo affordance and genuinely useful; the classifier and sequencer are already source-agnostic. |
| 7 | **Payment-update token** | getpaidhq | A signed link letting a customer replace a dead instrument. Our `payment_update` channel currently maps to a plain link, which reaches the same end by a weaker route. A real version needs Razorpay tokenisation. |
| 8 | **Recovery analytics over time** | emp-billing `/metrics`, dunlo | We have a funnel for one moment. Recovery rate *by decline class, by attempt number, over weeks* is what tells an operator whether the schedule is right. Blocked on volume more than on code. |
| 9 | **Per-merchant configuration** | recoup, getpaidhq | Multi-tenancy: per-merchant schedules, templates, credentials. Correct architecture, no value on a single-account demo. |
| 10 | **Gateway adapter interface** | recoup, getpaidhq, emp-billing | All three isolate provider logic behind one interface. We have a partial version — `SOURCE_ALIASES` and `declines.yaml` normalise Razorpay's vocabulary at the boundary — but `execution/razorpay.py` is called directly. Worth doing if a second PSP ever appears; premature otherwise. |

### Tier 3 — considered and declined

| feature | from | why not |
|---|---|---|
| **Customer tiering** (VIP / trial / standard cadences) | ajithmanmu | We have no tier data on this account. Inventing one is decoration, and it would put a plausible-looking knob in the config that no evidence supports. Declined in the first review too; still right. |
| **Envelope encryption for gateway credentials** | recoup | AES-GCM with per-key versioning. We hold test keys in `.env` and the code refuses any key not prefixed `rzp_test_`. Real for production, theatre here. |
| **Non-custodial architecture** | recoup | Funds never flow through the recovery system. Already true of ours — Razorpay holds the money. |
| **Credit notes, proration, usage metering** | getpaidhq, emp-billing | Billing-platform features. We are a recovery engine, not a biller. |
| **CLI covering every endpoint** (`gphq`) | getpaidhq | We have scripts. A second interface over the same functions is surface, not capability. |
| **n8n / Step Functions orchestration** | Etherlabs, ajithmanmu | Both externalise scheduling to a workflow engine. Our scheduling is ~40 lines of pure state machine with tests, and moving it into YAML would trade testability for a diagram. |

---

## What this review says about the codebase

The four dead controls share a shape: each was written for the **one-shot batch
path**, and none was carried across when the product became a sequencer. The
config still declared them, the function signatures still accepted them, and the
tests never asked whether anything read them.

The general lesson is that a parameter threaded through three functions looks
exactly like a working control right up until you check the caller. The specific
fix is that every control now has a test asserting it fires *from config* — not
that the function honours the argument, which was always true and always
useless.
