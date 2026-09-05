# Why the recovery rate is what it is, and what 70% would take

Written 28 Aug 2026, in response to a direct challenge: *"isn't it shitty
recovery, need at least 70% recovery rate"*.

The challenge was right that the number needed answering. The answer is not the
one either of us expected, and it changed what we built.

---

## 1. The measured number, and what its denominator contains

From the live batch, before the sequencer existed:

```
recovered / EVERYTHING at risk          Rs 28,433 / Rs 2,52,576  = 11.3%
recovered / revenue we CHOSE to chase   Rs 28,433 / Rs 1,75,447  = 16.2%
links paid / links sent                 1 of 15                  =  6.7%
```

Two things about that denominator matter more than the ratio:

- **Rs 77,129 of it is revenue we deliberately declined to chase** — below the
  exposure floor. That floor is a *policy choice*, not a break-even: messaging
  costs tens of paise, so the point below which chasing genuinely loses money is
  around Rs 15–100. The floor sits far above it because a contact also spends a
  customer's tolerance and the merchant's sender reputation. See
  `src/recovery/economics.py`; an earlier version of this document claimed the
  floor *was* break-even, and that was wrong by two orders of magnitude.
- **14 of the 15 links were never shown to a customer.** One human clicked one
  link. That is not a recovery rate; it is a sample of size one.

So 11.3% is not a measurement of how well the system recovers. It is a
measurement of how much money was at risk on a test account where nobody was
shopping.

## 2. The 70% target is real, and it belongs to a different book

Published benchmarks, from third parties, cited in full at the end:

| population | recovery |
|---|---|
| abandoned checkout | 3–8% typical, 10–14% leaders, 15–22% top performers |
| failed payments, no automation | 20–31% |
| failed payments, basic dunning | 45–55% |
| **failed payments, comprehensive dunning** | **70–80%** |

Two things follow immediately.

**70% of abandoned carts is not a real number.** Nobody achieves it. Our live
batch is 15 abandoned checkouts and 1 failed payment — overwhelmingly the
stream where the ceiling is around 20% for the best operators in the world.

**70–80% of failed payments is a real number** — and every source for it is a
*subscription billing* company: Baremetrics, Recurly, Chargebee. That turned
out to be the whole point.

## 3. What the sequencer is, and why one attempt caps you at ~25%

The system did **one** bounded intervention per unit of revenue at risk. Every
production dunning system in [PRIOR_ART.md](PRIOR_ART.md) instead runs a
**scheduled sequence over days**, classified by *why* the payment failed. That
is the entire structural difference between the 20–31% band and the 70–80% one:
the rate compounds across attempts spaced far enough apart for the underlying
cause to have changed.

Spacing is the load-bearing part. Retrying a declined card thirty seconds later
fails for the same reason it failed the first time; retrying it on payday does
not.

So we built one — `src/recovery/dunning.py`, driven by `config/declines.yaml`,
with Razorpay's own error `reason` strings mapped to six decline classes and a
schedule per class. Hard declines are never retried by anyone, and now not by
us either.

## 4. The result: `python -m scripts.recovery_curve`

One free parameter — the per-attempt conversion rate among recoverable items —
fitted so a **single** attempt on a checkout book lands at the bottom of the
published no-automation band. That fitted value then predicts two books, with
no further tuning. Fitting on one published quantity and predicting a different
one is the whole design; a model tuned until it printed 70% would be worthless.

```
FITTED  per-attempt conversion among recoverable items: 0.256

BOOK A  one-off checkout, no mandate   <- THIS ACCOUNT
  recoverable ceiling  74.0%
  after 1 attempt      19.9%
  after 3 attempts     44.4%
  FULL LADDER          44.4%   below the published 70%-80%

BOOK B  recurring subscription, mandate present
  recoverable ceiling  79.7%
  after 1 attempt      22.3%
  after 7 attempts     68.4%
  FULL LADDER          68.4%   at the edge of the published band
```

Book B reaches the band once the silent-retry series is 6 rather than 4 (71.6%)
or the hard-decline share is 5% rather than 16% (75.8%) — both inside the range
those systems describe. **Book A reaches it under none of the variations
tested.**

The model was never shown the 70–80% figure. Reproducing it on the subscription
book and not on the checkout book is the finding.

### Why the two books differ

**The mix.** A subscription book is soft-decline dominated: ~58% insufficient
funds against a card that will work next week. A checkout book also carries
authentication failures, wrong UPI handles, and customers who actively
cancelled — none of which a retry can fix at any cadence. Recoverable ceiling:
80% vs 74%.

**The mandate**, which is the larger term. With a saved token or e-mandate a
retry costs nothing and interrupts nobody, so a subscription book runs four or
more *silent* attempts before it ever messages anyone. Without one, every
attempt spends a customer contact, and the compliance ceiling caps those at 3.
**Strip the mandate from Book B and it falls from 68.4% to 49.7%** — the single
largest term in the model.

## 5. The actionable version

The route to 70% on a Razorpay account is **not more messages**. The contact
ceiling binds first, and pushing past it is how a sender reputation dies.

It is holding a **mandate** — UPI Autopay, e-mandate, or card tokenisation at
checkout. That converts the cheapest and least intrusive attempts from
unexecutable into free. It is also a capability Razorpay already sells, which
makes it a recommendation a merchant can act on rather than an excuse.

On this account we have neither tokens nor mandates, so every `silent` step in
`config/declines.yaml` is correctly reported as **UNEXECUTABLE** rather than
modelled as though it ran. That honesty is what makes the 44.4% figure for Book
A mean something.

## 6. What we would say to a judge

- Measured recovery on the live batch, stated with its full denominator and its
  sample size of one. No adjustment.
- A retry sequencer that is real code, running on the live account, with the
  two canonical retry bugs as named regression tests.
- An explicit account of where 70% comes from, why this book cannot reach it,
  and the one capability that would change that.

We would not quote 70%. Razorpay sees recovery data across thousands of
merchants, and a number lifted from someone else's book is the fastest way to
lose that room.

---

## Sources

Third-party benchmarks, none of them ours:

- [Subscription payment recovery benchmarks — Baremetrics](https://baremetrics.com/blog/subscription-payment-recovery-benchmarks)
- [Failed payment recovery: a data-based strategy — Recurly](https://recurly.com/blog/failed-payment-recovery-data-based-strategy/)
- [Dunning management for SaaS — Chargebee](https://www.chargebee.com/blog/dunning-management-for-saas-business/)
- [Abandoned cart benchmarks — Klaviyo](https://www.klaviyo.com/blog/abandoned-cart-benchmarks)
- [Average abandoned cart recovery rates — Sendtric](https://www.sendtric.com/average-abandoned-cart-recovery-rates-2026/)
- [Razorpay payment error codes](https://razorpay.com/docs/errors/payments/list/)
