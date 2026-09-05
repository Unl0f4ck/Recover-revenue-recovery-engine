"""Reading what a customer wrote back.

The gap this closes. Promise-to-pay needs an operator to read "can I pay
Friday?" and type a date. Opt-out needs someone to notice "stop texting me" and
run a CLI command. Both features are built, tested and wired -- and both sit
behind a human doing text comprehension in the middle of an otherwise automatic
loop. That is the one job here a language model is genuinely better at than a
regex, because the ways people say "Friday" are unbounded.

THE MODEL PROPOSES. THE ENGINE DECIDES.

Nothing in this module acts. It returns a `Reading` -- an intent, an optional
date, a confidence -- and the caller hands that to the same deterministic
functions an operator's typing goes through. `promises.record` still refuses a
date beyond the horizon, still refuses a past date, still refuses a debtor who
has broken two promises. `suppression.suppress` still writes an append-only
record. A hallucinated date is rejected by exactly the code that rejects a typo.

So the worst a wrong reading can do is propose something that gets refused, or
route a case to a human. It cannot send a message, skip a guard, or move money.

WHY A CONFIDENCE FLOOR AND NOT A BINARY. The expensive mistake here is not
misreading a date -- the horizon check catches an absurd one. It is silently
treating "I already paid this, check your records" as a promise, which pauses
the ladder for a week on a case that needed a human immediately. Anything the
model is unsure about, and anything it reads as a dispute, goes to review rather
than being acted on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .provider import LLMUnavailable, Provider, Result, get_provider

# What a reply can mean, in the vocabulary the engine already has.
PROMISE = "promise_to_pay"       # "I'll pay Friday"      -> promises.record
OPT_OUT = "opt_out"              # "stop texting me"      -> suppression.suppress
PAID = "already_paid"            # "I paid this already"  -> reconcile, then review
DISPUTE = "dispute"              # "this invoice is wrong"-> review, stop chasing
QUESTION = "question"            # "which invoice?"       -> review
UNCLEAR = "unclear"              # -> review, never guess

ACTIONABLE = {PROMISE, OPT_OUT}

# Minimum confidence before a reading is acted on automatically. Below this the
# case goes to a person, which is the correct expensive-but-safe default: a
# missed promise costs one unnecessary reminder, a wrongly-inferred one pauses
# collection on a live debt for a week.
MIN_CONFIDENCE = 0.75

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [PROMISE, OPT_OUT, PAID, DISPUTE, QUESTION, UNCLEAR],
        },
        "pay_by": {
            "type": ["string", "null"],
            "description": ("ISO date YYYY-MM-DD the customer says they will "
                            "pay, resolved against the reference date given. "
                            "null unless intent is promise_to_pay."),
        },
        "confidence": {"type": "number",
                       "description": "0 to 1, how sure you are of the intent"},
        "quote": {"type": "string",
                  "description": "the words that carry the meaning, verbatim"},
        "reasoning": {"type": "string",
                      "description": "one short sentence, for the audit trail"},
    },
    "required": ["intent", "confidence", "quote", "reasoning"],
}

SYSTEM = (
    "You read short replies from customers about an unpaid payment, invoice or "
    "abandoned checkout, and classify what the customer means. Indian English, "
    "Hindi and Hinglish are all common; so are one-word replies.\n"
    "Rules:\n"
    "- Only use promise_to_pay when the customer commits to paying. A vague "
    "'soon' or 'will see' is not a promise; return unclear.\n"
    "- Resolve relative dates ('Friday', 'kal', 'next week', 'month end') "
    "against the reference date you are given, and return an ISO date.\n"
    "- 'STOP', 'do not contact me', 'unsubscribe', 'band karo' are opt_out.\n"
    "- If the customer says they have already paid, that is already_paid, not "
    "a promise.\n"
    "- If they contest the amount or say the invoice is wrong, that is dispute.\n"
    "- Be honest with confidence. A low number sends this to a person, which is "
    "the right outcome when the reply is ambiguous."
)


@dataclass
class Reading:
    """What the model thinks the customer meant. A proposal, never a decision."""
    intent: str
    confidence: float
    quote: str = ""
    reasoning: str = ""
    pay_by: datetime | None = None
    provider: str = ""
    model: str = ""
    text: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """Confident enough, and an intent the engine can act on by itself."""
        return (self.intent in ACTIONABLE
                and self.confidence >= MIN_CONFIDENCE)

    @property
    def needs_human(self) -> bool:
        return not self.actionable

    def why(self) -> str:
        if self.intent in ACTIONABLE and self.confidence < MIN_CONFIDENCE:
            return (f"read as {self.intent} but only {self.confidence:.0%} "
                    f"sure, so a person should look")
        if self.intent == PAID:
            return "says they already paid; reconcile before chasing again"
        if self.intent == DISPUTE:
            return "disputes the amount; stop chasing and let a person answer"
        if self.intent == QUESTION:
            return "asked a question; a person should answer it"
        if self.intent == UNCLEAR:
            if self.model == "unavailable":
                return f"nobody read this yet -- {self.reasoning}"
            return "could not tell what they meant"
        return self.reasoning


def read_reply(text: str, now: datetime, provider: Provider | None = None,
               reference: str = "", amount_paise: int = 0) -> Reading:
    """Classify one customer reply. Never acts on it."""
    p = provider or get_provider()
    prompt = (
        f"Reference date (today): {now:%Y-%m-%d} ({now:%A}).\n"
        + (f"This concerns {reference}" +
           (f", amount Rs {amount_paise/100:,.0f}" if amount_paise else "") +
           ".\n" if reference else "")
        + f"\nCustomer reply:\n\"\"\"\n{text.strip()}\n\"\"\""
    )
    res: Result = p.json_call(prompt, SCHEMA, SYSTEM)
    d = res.data

    pay_by = None
    raw_date = d.get("pay_by")
    if raw_date:
        try:
            pay_by = datetime.fromisoformat(str(raw_date)).replace(
                tzinfo=now.tzinfo)
        except ValueError:
            # A date we cannot parse is not a date. Downgrade rather than drop
            # the reading: the intent may still be right and a person can read
            # the quote.
            pay_by = None

    intent = str(d.get("intent") or UNCLEAR)
    if intent == PROMISE and pay_by is None:
        # A promise with no usable date cannot pause anything, so it is not
        # actionable -- but it is still worth a person's attention.
        intent = UNCLEAR

    try:
        conf = float(d.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0

    return Reading(
        intent=intent, confidence=max(0.0, min(conf, 1.0)),
        quote=str(d.get("quote", ""))[:300],
        reasoning=str(d.get("reasoning", ""))[:300],
        pay_by=pay_by, provider=res.provider, model=res.model,
        text=text.strip()[:500])


def unreadable(text: str, why: str) -> Reading:
    """A reply we could not get a reading for at all.

    Not an error to raise at the operator. The free tier this runs on caps
    requests per model per day -- twenty on the newest alias -- so being unable
    to reach a model is an ordinary Tuesday, not an outage. What matters is
    where the reply goes when that happens.

    It goes to a person. A reply nobody read is exactly the case a human queue
    exists for, and the alternative failure is the one that actually costs
    something: a customer writes "STOP", the quota is gone, the command exits
    with a traceback, and the ladder keeps messaging them tomorrow. That is a
    compliance failure dressed as a stack trace.

    So an unreachable model produces the same thing an ambiguous reply does --
    an UNCLEAR reading at zero confidence, which `apply_reading` routes to
    review and which can act on nothing by construction.
    """
    return Reading(intent=UNCLEAR, confidence=0.0, provider="none",
                   model="unavailable", reasoning=why,
                   text=(text or "").strip()[:500])


# ---------------------------------------------------------------------------

@dataclass
class Applied:
    """What the deterministic layer did with the proposal."""
    reading: Reading
    action: str                  # promised | suppressed | escalated | refused
    detail: str = ""


def apply_reading(r: Reading, reference: str, now: datetime,
                  cfg: dict | None = None, promise_path=None,
                  sup_path=None, review_path=None,
                  who: str = "reply-reader", amount_paise: int = 0,
                  customer_ref: str | None = None) -> Applied:
    """Hand the proposal to the code that validates everything else.

    Note what this function does NOT contain: any judgement. The horizon check,
    the broken-promise ceiling and the future-date rule all live in
    `promises.record` and apply identically whether the date came from a model
    or from an operator's keyboard.

    TWO DIFFERENT KEYS, and conflating them was a real bug here. A promise is
    about a CASE -- this invoice, paused until Friday. An opt-out is about a
    PERSON -- never contact them again, on any case, forever. Passing the
    invoice id to the suppression list would either fail loudly (it did) or,
    worse, suppress a string nobody will ever match, leaving the customer to be
    messaged again by every other campaign they appear in.
    """
    from ..recovery import promises, review, suppression

    if not r.actionable:
        review.claim(reference, who, now,
                     f"customer replied: {r.quote or r.text[:120]} — {r.why()}",
                     path=review_path)
        return Applied(r, "escalated", r.why())

    if r.intent == OPT_OUT:
        who_said = suppression.normalise(customer_ref)
        if not who_said:
            # Somebody asked us to stop and we do not know who they are. That
            # is a person's problem to solve, not one to silently drop -- and
            # certainly not one to solve by suppressing an invoice number.
            review.claim(reference, who, now,
                         f"customer asked to stop but no contact identity is "
                         f"attached to this case: {r.quote[:100]}",
                         path=review_path)
            return Applied(r, "escalated",
                           "asked to stop, but we do not know which contact "
                           "to suppress")
        suppression.suppress(who_said, now,
                             reason=f"replied: {r.quote[:120]}",
                             action=suppression.OPT_OUT, source="customer-reply",
                             path=sup_path)
        return Applied(r, "suppressed",
                       f"{who_said} will not be contacted again")

    try:
        p = promises.record(reference, r.pay_by, now, amount_paise=amount_paise,
                            channel="reply", note=r.quote[:160],
                            recorded_by=who, cfg=cfg, path=promise_path)
    except promises.PromiseRefused as e:
        # The model proposed something the rules do not allow. Refused, and
        # routed to a person -- exactly what happens when an operator types it.
        review.claim(reference, who, now,
                     f"promise refused: {e}. Reply: {r.quote[:120]}",
                     path=review_path)
        return Applied(r, "refused", str(e))

    return Applied(r, "promised",
                   f"paused until {p.pay_by:%d %b}")
