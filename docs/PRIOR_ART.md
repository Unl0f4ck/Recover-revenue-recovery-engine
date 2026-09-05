# Prior art: how payment recovery systems are actually built

Extracted 28 Aug 2026 from eight open-source repositories and two Medusa bug
reports, to answer one question before writing any code:

> **What does a production dunning system do that our one-shot recovery loop does not?**

The short answer, which the rest of this document supports: it does not make
one recovery attempt. It runs a **scheduled sequence of attempts over days**,
classified by *why* the payment failed, escalating from silent retry to
customer contact, with a hard stop. Every source below agrees on that shape.
Our system did one attempt. That single structural difference is the whole gap
between a ~20% recovery rate and a ~70% one.

---

## 1. The eight repositories

### 1.1 `Etherlabs-dev/payment_recovery_engine`

Python policy core + n8n orchestration, Stripe webhooks in, PostgreSQL ledgers out.

The most directly comparable design, and the only one that publishes concrete
intervals:

| decline class | attempts | schedule |
|---|---|---|
| insufficient funds | 3 | **48h, 120h, 168h** |
| temporary processing failure | 3 | **1h, 6h, 24h** |
| hard decline, auth failure, fraud, expired method | **0** | manual review / notify only |

Three ledgers — `events`, `retries`, `notifications`. Retry leases are
`FOR UPDATE SKIP LOCKED` with unique idempotency keys. Every decision record
carries retry permission, next retry, budget, notification, manual review,
reason, and policy version.

Deliberately **no ML** — "deterministic policy rules embedded in code logic".
And notably, it refuses to quote a recovery rate: "This does not claim a
recovery rate, revenue outcome, time saving, production SLA, or client result."

> **Taken:** the two-speed schedule (funds failures are slow, technical
> failures are fast), the stamped `policy_version`, and the refusal to quote a
> number the system has not measured.

### 1.2 `ajithmanmu/dunning-system`

AWS Step Functions state machine; Stripe webhook to API Gateway to Lambda to
Step Functions. Three DynamoDB tables: idempotency, customer tiers, dunning state.

Tiered schedules — the only source that varies cadence by customer value:

| tier | schedule |
|---|---|
| hard decline (`stolen_card`, `lost_card`, `do_not_honor`, `pickup_card`) | **immediate cancel, no retries** |
| VIP | day 1, 3, 7 then cancel |
| trial | day 1, 3 then cancel |
| standard | day 3, 7, 14 then cancel |

Terminates on `PaymentRecovered` at any point, or cancellation after exhaustion.

> **Taken:** the escalation ladder terminating in an explicit stop state, and
> the hard-decline short circuit. **Not taken:** customer tiering — we have no
> tier data on the live account and inventing one would be decoration.

### 1.3 `merrttopal/recoup`

Java 21 / Spring Modulith / PostgreSQL. The most rigorous on correctness.

Pipeline: ingest, classify (soft/hard/unknown), retry, reconcile, recover.

Three ideas worth stealing outright:

1. **At most one success per invoice, enforced by a partial unique index.** Not
   by application logic — by the schema. Double-charging is made
   *unrepresentable*.
2. **Write-ahead `conversation_id`, committed before the gateway call.** The
   idempotency key exists in durable storage before anything is sent, so a
   crash mid-call cannot lose it.
3. **Reconcile, do not re-charge.** "On timeout or ambiguous response the
   engine asks the gateway for the truth (`retrieve`) instead of retrying
   blindly."

Provider decline codes are normalised to a shared vocabulary at the boundary.
State changes append to an immutable ledger with no update/delete surface.

> **Taken:** all three. Point 3 is the direct fix for Medusa #16292 below.

### 1.4 `FrekiManagarm/dunlo-v2`

TanStack Start / Drizzle / Neon / Trigger.dev. Stripe monitoring SaaS:
detection, retry, branded dunning email, recovered-revenue analytics.
Email providers pluggable (Postmark, Resend, Mailgun, SendGrid).

Claims "an intelligent schedule" but publishes no intervals, no max attempt
count, and no escalation triggers.

> **Taken:** the pluggable notification-channel boundary. Nothing else — the
> retry logic is unspecified, which is itself instructive: a dunning product
> can ship without ever writing its schedule down, and reviewers will not
> notice.

### 1.5 `getpaidhqco/getpaidhq`

Go 1.26, hexagonal ports-and-adapters, PostgreSQL + Hatchet-lite durable
workflows. Paystack and Checkout.com adapters.

Dunning is a first-class module: recovery campaigns that "retry failed charges
on a schedule, with configurable scope, customer communications,
payment-update tokens so customers can fix their own details, and dunning
analytics." States plainly that "idempotent payment handling means retries
never double-charge."

> **Taken:** the **payment-update token** — a signed link letting the customer
> fix their own card. This is the correct terminal escalation for a *hard*
> decline, where retrying is pointless by definition but the customer can still
> pay. It is what a hard decline should escalate *to*, rather than straight to
> write-off.

### 1.6 `killbill/killbill-payment-retries-plugin`

Kill Bill payment-control plugin. Classifies via an `ErrorMessage` enum
(`INSUFFICIENT_FUNDS` among them) and exposes endpoints filtered by
`retryable=true` or a specific `errorMessage`. Also verifies the state of the
payment method behind a failed payment via `paymentExternalKey`.

Retry scheduling itself lives in Kill Bill core, not the plugin; the plugin's
job is **retryability classification only**.

> **Taken:** the architectural separation. *Deciding whether an error is
> retryable* is a different concern from *deciding when to retry it*, and they
> should be separately testable. Our `declines.py` / `dunning.py` split follows
> this.

### 1.7 `EmpCloud/emp-billing`

Express 5 / TypeScript / BullMQ + Redis, 30+ tables, plugin gateway interface
across Stripe, Razorpay and PayPal. Has a `dunning_attempts` table and
`/dunning` routes for retry history and schedule management.

Invoice state machine: draft, sent, partial, paid, **overdue (triggers
dunning)**, voided/written-off.

> **Taken:** `dunning_attempts` as an explicit persisted entity with its own
> history, rather than retry state smeared across the payment record. Also the
> named terminal state — `written_off` — so an exhausted sequence resolves
> rather than lingering.

### 1.8 `razorpay/razorpay-node`

The official SDK, read for the API surface actually available to us: Payments,
Orders, Refunds, Customers, **Tokens**, Invoices, **Subscriptions**, Payment
Links, Settlements, Disputes, Cards, QR Code, **Emandate**, **Paper NACH**,
UPI, Virtual Accounts, Fund, Account, Addon, Item, Stakeholder, Documents,
OAuth. Methods follow `instance.{resource}.{method}(id[, params])`.

> **Relevant:** `Tokens` + `Subscriptions` + `Emandate`/`Paper NACH` are what
> a true server-side re-charge would need — a saved token to charge against.
> On a fresh test account with no saved tokens and no mandates, **we cannot
> re-present a card without the customer.** This constrains what our sequencer
> may honestly claim to execute, and is recorded as such in `declines.yaml`
> rather than quietly modelled.

---

## 2. The two Medusa issues, as test cases

These are not illustrations. They are the two ways a retry sequencer fails in
production, and both are now named regression tests in `tests/test_dunning.py`.

### 2.1 medusajs/medusa#16292 — retry regenerates the idempotency key

**Failure.** `PaymentModuleService.capturePayment` creates a `Capture` row,
forwards **that row's ID** to the provider as the idempotency key, and — if the
provider call fails — *deletes the row* and rethrows. The retry creates a
brand-new `Capture` row with a new ID, hence **a new idempotency key the
provider has never seen**. The provider cannot deduplicate. The customer is
charged twice.

The mechanism is exact and worth restating: the system did everything right
except *persist the key across the failure*. Idempotency that is discarded on
error is not idempotency — it is idempotency for the happy path only, which is
the one path that never needed it.

**Test:** `test_medusa_16292_transport_failure_reuses_idempotency_key` —
a transport-level failure of attempt *N* must re-issue attempt *N* **with the
same key**, and must NOT advance the sequence to *N+1*. Advancing the attempt
counter is precisely what mints a new key.

### 2.2 medusajs/medusa#16398 — terminal failure webhooks are dropped

**Failure.** The Stripe provider correctly maps `payment_intent.canceled` to
`PaymentActions.CANCELED` and `payment_intent.payment_failed` to
`PaymentActions.FAILED`. But the core subscriber returns early on both —
"We currently don't handle these payment statuses in the processPayment
function" — and `processPaymentWorkflow` branches only on `SUCCESSFUL` and
`AUTHORIZED`.

Result: Stripe says canceled, Medusa says pending, **forever**. Inventory stays
reserved, orders never resolve, and a human has to clean up.

For a dunning system this is the worse of the two bugs. A recovery sequence
that cannot observe a terminal state **never terminates** — it keeps escalating
against a customer whose payment is already definitively dead, which is exactly
the behaviour that gets a merchant's messaging channel blocked.

**Test:** `test_medusa_16398_terminal_state_stops_the_sequence` — every
terminal outcome, including the ones nobody remembers to handle
(cancelled-by-customer, hard decline), must move the sequence to a stop state
and record a reason. There is no path that leaves a sequence live.

---

## 3. What every source agrees on

Convergent design, adopted wholesale:

1. **Classify before retrying.** Hard declines are never retried by anyone.
2. **Schedule in days, not seconds.** Retrying a declined card 30 seconds later
   fails for the same reason it failed the first time.
3. **Two speeds.** Funds problems resolve on the customer's payday; technical
   problems resolve in hours.
4. **Escalate silent, then contact.** Attempt before you ask; ask before you give up.
5. **Terminate explicitly.** Recovered, exhausted, or written off — never open.
6. **Idempotency key minted before the call, persisted, reused.**
7. **On ambiguity, read the gateway. Never re-charge.**
8. **Append-only ledger, policy version stamped.**

## 4. Where we deliberately differ

- **No customer tiering.** We have no tier data; inventing one is decoration.
- **Diagnosis drives rail choice.** No source above knows *why* a segment is
  failing. Ours does — the attribution ladder is already built — so a retry
  under `psp_degradation` switches PSP rather than repeating a rail that is
  measurably broken. That is what the existing diagnosis work buys, applied to
  a metric that matters.
- **We do not claim we can re-present a card.** No saved tokens, no mandates on
  this account. Attempts that would require a server-side charge are marked
  `requires_mandate` and reported as unexecutable rather than modelled.
