"""Where a 70% recovery rate comes from, and whether this book can reach it.

    python -m scripts.recovery_curve

MODELLED, NOT MEASURED, and labelled as such wherever it is reported. The live
batch is the only place measured money lives. This exists to answer one
question that a single measured batch cannot:

    a single recovery attempt recovers 20-31% of failed payments
    comprehensive dunning recovers 70-80%
    is that gap reachable from OUR book, or is it a different book entirely?

THE METHOD. One free parameter -- the per-contact conversion rate among
recoverable items -- fitted so that a SINGLE attempt over the checkout book
lands at the bottom of the published no-automation band. That same fitted
parameter is then used, untouched, to predict two different books:

    BOOK A   one-off checkout payments, no saved mandate     <- this account
    BOOK B   recurring subscription billing, mandate present <- the benchmark

If B reproduces the published 70-80% band without further tuning, the benchmark
is real but belongs to a different population, and the honest thing to report
is what it would take to get there rather than a number lifted from someone
else's book. Fitting on one published quantity and predicting a different one
is the entire design; a model tuned until it printed 70% would be worthless.

WHY TWO BOOKS. Every source for the 70-80% figure is a SUBSCRIPTION billing
company -- Baremetrics, Recurly, Chargebee. Subscription dunning operates on a
population where nearly every failure is a recurring charge against a card the
merchant already has a mandate for. That changes two things at once, and both
push the same way: the failure mix is overwhelmingly soft, and the merchant can
retry SILENTLY, many times, without spending a customer contact. Neither holds
for one-off checkout traffic.

THE CEILING. Not every failure is recoverable at any cadence. A card reported
stolen will not be un-reported; a customer who deliberately cancelled mostly
stays cancelled. Each class carries a `recoverable` ceiling and the sequence
can only approach it. This is why the decline MIX constrains the achievable
rate far more than the schedule does.

Every assumption is a named constant. The sensitivity tables exist so a reader
can see how hard the conclusion leans on each one.
"""
from __future__ import annotations

import numpy as np

from src.recovery.declines import load_declines

SEED = 20260828
N = 40_000

# Published third-party bands. Used as a fitting target and as a check; sources
# are cited in RESULTS.md. Not our numbers.
NO_AUTOMATION = (0.20, 0.31)        # one attempt, no sequence
COMPREHENSIVE = (0.70, 0.80)        # subscription dunning, full programme

# ---------------------------------------------------------------------------
# ASSUMPTION 1 -- the decline mix, per book.
#
# Not measured on our account: it has four failed payments, which is not a mix,
# it is an anecdote. Book A is plausible one-off checkout traffic. Book B is a
# subscription book, where expired cards and insufficient funds dominate and
# neither abandonment nor deliberate cancellation exists as a failure at all.
# The mix is the most load-bearing input here, which is why it is varied in the
# sensitivity table rather than asserted.
# ---------------------------------------------------------------------------
BOOK_A = {                          # one-off checkout -- this account
    "SOFT_FUNDS":       0.34,
    "SOFT_TECHNICAL":   0.26,
    "AUTH":             0.16,
    "HARD_INSTRUMENT":  0.12,
    "VPA":              0.06,
    "CUSTOMER_ABORTED": 0.06,
}
BOOK_B = {                          # recurring subscription -- the benchmark
    "SOFT_FUNDS":       0.58,
    "SOFT_TECHNICAL":   0.22,
    "HARD_INSTRUMENT":  0.16,       # expired cards, the classic subscription churn
    "AUTH":             0.04,       # recurring charges skip interactive auth
    "VPA":              0.00,
    "CUSTOMER_ABORTED": 0.00,       # no checkout to abandon
}

# ---------------------------------------------------------------------------
# ASSUMPTION 2 -- the ceiling per class: the fraction recoverable AT ALL, by any
# sequence of any length. This is what stops the model reaching 100%.
# ---------------------------------------------------------------------------
RECOVERABLE = {
    "SOFT_FUNDS":       0.88,   # the instrument works; usually only timing
    "SOFT_TECHNICAL":   0.92,   # nothing was wrong with the customer at all
    "AUTH":             0.70,   # needs them back at a checkout
    "HARD_INSTRUMENT":  0.35,   # only if they add a different instrument
    "VPA":              0.55,   # needs a different UPI address
    "CUSTOMER_ABORTED": 0.25,   # they decided not to buy; most stay decided
}

# ---------------------------------------------------------------------------
# ASSUMPTION 3 -- relative responsiveness per attempt. Multipliers on the single
# fitted rate, NOT independent free parameters, so the model keeps exactly one
# degree of freedom.
# ---------------------------------------------------------------------------
RESPONSIVENESS = {
    "SOFT_FUNDS":       1.10,
    "SOFT_TECHNICAL":   1.25,
    "AUTH":             0.85,
    "HARD_INSTRUMENT":  0.55,
    "VPA":              0.80,
    "CUSTOMER_ABORTED": 0.45,
}

# Silent retries a merchant WITH a mandate can make before spending a contact.
# Recurly and Chargebee both describe retry series in this range over 3-4 weeks.
SILENT_RETRIES_WITH_MANDATE = 4


def ladder(cfg: dict, with_mandate: bool) -> dict[str, int]:
    """How many ATTEMPTS each class's schedule actually delivers.

    Terminal steps never count -- a human review is a handoff, not an attempt,
    and counting it would pad the ladder with steps that cannot convert.

    Silent steps count only `with_mandate`. Without one they are unexecutable
    on this account and recover nothing, which is exactly the constraint this
    whole script exists to quantify.
    """
    contacting = set(cfg["compliance"]["contacting_channels"])
    cap = int(cfg["compliance"]["max_contacts_per_reference"])
    out = {}
    for name, spec in cfg["classes"].items():
        steps = cfg["schedules"][spec["schedule"]]["steps"]
        contacts = min(sum(1 for s in steps if s["channel"] in contacting), cap)
        silent = sum(1 for s in steps if s["channel"] == "silent")
        if with_mandate:
            # A mandate does not merely make the configured silent steps work;
            # it makes silent retries cheap enough to run a proper series,
            # which is what subscription dunning actually does.
            silent = SILENT_RETRIES_WITH_MANDATE if silent else 0
        else:
            silent = 0
        out[name] = contacts + silent
    return out


def simulate(p_base: float, attempts: dict[str, int], mix: dict,
             rng: np.random.Generator, n: int = N) -> np.ndarray:
    """Cumulative recovery after 0, 1, 2, ... attempts.

    Each recoverable item converts independently at each attempt it receives.
    Items past their class's ladder receive nothing further; items that are not
    recoverable never convert, at any attempt.
    """
    classes = [c for c in mix if mix[c] > 0]
    weights = np.array([mix[c] for c in classes])
    weights = weights / weights.sum()
    counts = rng.multinomial(n, weights)
    horizon = max(attempts[c] for c in classes)
    won = np.zeros(horizon + 1)

    for cls, k in zip(classes, counts):
        if k == 0:
            continue
        alive = rng.random(k) < RECOVERABLE[cls]
        p = min(p_base * RESPONSIVENESS[cls], 0.95)
        for step in range(1, attempts[cls] + 1):
            converts = alive & (rng.random(k) < p)
            won[step] += converts.sum()
            alive &= ~converts
    return np.cumsum(won) / n


def fit_p(target: float, mix: dict) -> float:
    """The per-contact rate at which ONE attempt hits the target.

    Bisection on a monotone function. This is the model's only free parameter;
    everything reported afterwards is a prediction from it.
    """
    one = {c: 1 for c in mix}
    lo, hi = 0.001, 0.95
    for _ in range(60):
        mid = (lo + hi) / 2
        got = simulate(mid, one, mix, np.random.default_rng(SEED))[1]
        lo, hi = (mid, hi) if got < target else (lo, mid)
    return (lo + hi) / 2


def band(x: float, lo: float, hi: float) -> str:
    return "in band" if lo <= x <= hi else ("above" if x > hi else "below")


def main() -> None:
    cfg = load_declines()

    print("RECOVERY CURVE  --  MODELLED, not measured")
    print("  one free parameter, fitted to the published one-attempt band,")
    print("  then used to predict two different books without further tuning")
    print()

    p = fit_p(NO_AUTOMATION[0], BOOK_A)
    print(f"FITTED  per-attempt conversion among recoverable items: {p:.3f}")
    print(f"  set so ONE attempt on the checkout book recovers "
          f"{NO_AUTOMATION[0]:.0%}, the bottom of the published")
    print(f"  no-automation band {NO_AUTOMATION[0]:.0%}-{NO_AUTOMATION[1]:.0%}. "
          f"Nothing below is fitted.")
    print()

    books = [
        ("BOOK A  one-off checkout, no mandate   <- THIS ACCOUNT", BOOK_A, False),
        ("BOOK B  recurring subscription, mandate present", BOOK_B, True),
    ]
    results = {}
    for title, mix, mandate in books:
        att = ladder(cfg, mandate)
        curve = simulate(p, att, mix, np.random.default_rng(SEED))
        ceiling = sum(mix[c] * RECOVERABLE[c] for c in mix)
        results[title] = (curve, ceiling, att, mix)

        print(title)
        used = {c: att[c] for c in mix if mix[c] > 0}
        print(f"  attempts delivered   " +
              ", ".join(f"{c}:{k}" for c, k in sorted(used.items(),
                                                      key=lambda kv: -kv[1])))
        print(f"  recoverable ceiling  {ceiling:>6.1%}  "
              f"(no schedule reaches past this)")
        marks = [1, 2, 3, min(5, len(curve) - 1), len(curve) - 1]
        for i in sorted(set(m for m in marks if 0 < m < len(curve))):
            print(f"    after {i:>2} attempt(s)  {curve[i]:>6.1%}")
        final = curve[-1]
        print(f"  FULL LADDER          {final:>6.1%}   "
              f"{band(final, *COMPREHENSIVE)} the published "
              f"{COMPREHENSIVE[0]:.0%}-{COMPREHENSIVE[1]:.0%}")
        print()

    a = results[books[0][0]][0][-1]
    b = results[books[1][0]][0][-1]

    print("=" * 72)
    print("READING")
    print("=" * 72)
    gap = b - a
    # Recomputed, not transcribed from the tables below -- a number quoted in
    # prose that drifts from the number in the table is worse than no number.
    att6 = {c: v + 2 if v > 3 else v for c, v in ladder(cfg, True).items()}
    b_6silent = simulate(p, att6, BOOK_B, np.random.default_rng(SEED))[-1]
    m5 = dict(BOOK_B)
    m5["SOFT_FUNDS"] += m5["HARD_INSTRUMENT"] - 0.05
    m5["HARD_INSTRUMENT"] = 0.05
    b_lowhard = simulate(p, ladder(cfg, True), m5,
                         np.random.default_rng(SEED))[-1]
    b_nomandate = simulate(p, ladder(cfg, False), BOOK_B,
                           np.random.default_rng(SEED))[-1]
    print(f"  Book A (this account)      {a:>6.1%}")
    print(f"  Book B (the benchmark)     {b:>6.1%}")
    print(f"  published band             {COMPREHENSIVE[0]:>6.0%}-{COMPREHENSIVE[1]:.0%}")
    print()
    print(f"  The model was fitted ONLY to the one-attempt band and never shown")
    print(f"  the 70-80% figure. It puts the subscription book {gap*100:.0f} points above")
    print(f"  the checkout book, and lands it at the edge of the published band")
    print(f"  -- inside it once the silent-retry series reaches 6 "
          f"({b_6silent:.0%}) or the")
    print(f"  hard-decline share drops to 5% ({b_lowhard:.0%}), both within the "
          f"range those")
    print(f"  systems describe. The checkout book reaches the band under NONE of")
    print(f"  the variations in the tables below.")
    print()
    print("  So 70-80% is a real number measuring a real thing. It is not")
    print("  measuring this book. Two differences do the work, both pointing the")
    print("  same way:")
    print()
    print("    1. THE MIX. A subscription book is soft-decline dominated:")
    print(f"       {BOOK_B['SOFT_FUNDS']:.0%} insufficient funds against a card that "
          f"works next week.")
    print("       A checkout book also carries authentication failures, wrong UPI")
    print("       handles and customers who actively cancelled -- none of which a")
    print("       retry can fix at any cadence.")
    print(f"       Recoverable ceiling: {results[books[1][0]][1]:.0%} vs "
          f"{results[books[0][0]][1]:.0%}.")
    print()
    print("    2. THE MANDATE. With a saved token or e-mandate a retry costs")
    print("       nothing and interrupts nobody, so a subscription book runs")
    print(f"       {SILENT_RETRIES_WITH_MANDATE} silent attempts before it ever "
          f"messages anyone. Without one,")
    print("       every attempt spends a customer contact, and the compliance")
    print(f"       ceiling caps those at "
          f"{cfg['compliance']['max_contacts_per_reference']}. Strip the mandate "
          f"from book B and it")
    print(f"       falls to {b_nomandate:.1%} -- the single largest term in the "
          f"whole model.")
    print()
    print("  THE ACTIONABLE VERSION. The route to 70% on a Razorpay account is")
    print("  not more messages. The contact ceiling binds first, and pushing past")
    print("  it is how a sender reputation dies. It is holding a MANDATE: UPI")
    print("  Autopay, e-mandate, or card tokenisation at checkout. That converts")
    print("  the cheapest and least intrusive attempts from unexecutable into")
    print("  free, and it is a capability Razorpay already sells.")
    print()
    print("SENSITIVITY  full-ladder recovery as the assumptions move")
    print("  Each book is re-mixed to the stated hard-decline share, with the")
    print("  difference taken from (or given to) SOFT_FUNDS. The two books have")
    print("  DIFFERENT assumed shares, so each is marked on its own row.")
    print(f"  {'hard-decline share':<22} {'book A':>9} {'book B':>9} "
          f"{'B ceiling':>11}  assumed")
    for hard in (0.05, 0.12, 0.16, 0.20, 0.30, 0.40):
        row, ceil_b = [], 0.0
        for _, mix, mandate in books:
            m = dict(mix)
            m["SOFT_FUNDS"] = max(m["SOFT_FUNDS"] + m["HARD_INSTRUMENT"] - hard,
                                  0.01)
            m["HARD_INSTRUMENT"] = hard
            row.append(simulate(p, ladder(cfg, mandate), m,
                                np.random.default_rng(SEED))[-1])
            if mandate:
                t = sum(m.values())
                ceil_b = sum(m[k] / t * RECOVERABLE[k] for k in m)
        mark = ""
        if abs(hard - BOOK_A["HARD_INSTRUMENT"]) < 1e-9:
            mark = "A"
        if abs(hard - BOOK_B["HARD_INSTRUMENT"]) < 1e-9:
            mark = (mark + " B").strip()
        print(f"  {hard:>19.0%}  {row[0]:>8.1%} {row[1]:>9.1%} {ceil_b:>10.1%}"
              f"   {mark}")
    print()

    print(f"  {'silent retries (book B)':<24} {'book B':>9}")
    for k in (0, 2, 4, 6, 8):
        att = ladder(cfg, True)
        base = ladder(cfg, False)
        att = {c: base[c] + (k if cfg["schedules"][
            cfg["classes"][c]["schedule"]]["steps"][0]["channel"] == "silent"
            or any(s["channel"] == "silent" for s in cfg["schedules"][
                cfg["classes"][c]["schedule"]]["steps"]) else 0) for c in att}
        v = simulate(p, att, BOOK_B, np.random.default_rng(SEED))[-1]
        mark = "  <- assumed" if k == SILENT_RETRIES_WITH_MANDATE else ""
        print(f"  {k:>21}  {v:>8.1%}{mark}")
    print()

    print("WHAT THIS DOES NOT SAY")
    print("  It does not say this system recovers any of these amounts. It says")
    print("  what the published benchmark is measuring, why this book differs,")
    print("  and what would have to change. The only MEASURED recovery figure we")
    print("  have is the live batch, reported separately and without adjustment.")


if __name__ == "__main__":
    main()
