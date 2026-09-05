"""A synthetic batch, put through the real recovery engine.

WHAT IS SYNTHETIC AND WHAT IS NOT. This package invents exactly two things:

    1. the BOOK      -- which cases exist, their amounts, decline reasons
    2. the CUSTOMER  -- whether a contacted customer pays, opts out or ignores

Everything between those two is the production code path, unmodified. The
schedule comes from `config/declines.yaml`; the decision to contact comes from
`dunning.authorize_contact`; quiet hours, contact ceilings, opt-out checks,
promise pauses, per-run caps and the kill switch are the same guards the live
runner passes through; every event lands in the same append-only ledger via the
same `ledger.append`. Nothing here re-implements a rule, and nothing here is
allowed to skip one.

That boundary is the point. A simulator that models its own escalation logic
demonstrates only that its author can write a plausible loop. This one can only
report what the real engine does when a batch is put in front of it, which
means a bug in the engine shows up as a wrong number here rather than being
papered over.

WHAT THIS CAN AND CANNOT TELL YOU. It cannot measure a recovery rate. The rate
that comes out is a consequence of the per-contact conversion assumption that
goes in, and no amount of machinery in between turns an assumption into a
measurement. The one measured recovery number this project has is the Rs 28,433
on the live Razorpay account, read back from the API.

What it CAN tell you is everything the assumption does not determine: whether
the ladder terminates, whether the ceilings bind before the schedule does, how
much a batch costs to work, what the contribution is after that cost, how many
cases end in a human's queue rather than a resolution, and whether the audit
trail reconstructs. Those are properties of the engine, and on a synthetic book
they are as real as on a live one.
"""
