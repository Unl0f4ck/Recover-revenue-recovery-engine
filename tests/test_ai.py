"""Tests for the one place this system talks to a language model.

Every test runs on the deterministic stub: no key, no network. What is being
tested is OUR handling of a response, not a provider's behaviour -- which is why
the stub returns whatever the test hands it rather than pretending to be a model.

The property that matters most is the boundary: the model proposes, and the
deterministic engine still decides. A hallucinated date must be refused by
exactly the code that refuses a typo.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.ai import provider as P
from src.ai import replies as R

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=IST)


@pytest.fixture
def paths(tmp_path):
    return {"promise_path": tmp_path / "promises.jsonl",
            "sup_path": tmp_path / "suppression.jsonl",
            "review_path": tmp_path / "review.jsonl"}


def _cfg(**over):
    base = {"promise_to_pay": {"enabled": True, "grace_days": 1,
                               "max_horizon_days": 30, "max_broken": 2}}
    base["promise_to_pay"].update(over)
    return base


def _stub(**d):
    return P.Stub(response=d)


# ---------------------------------------------------------------------------
# provider plumbing
# ---------------------------------------------------------------------------

def test_a_placeholder_key_is_not_a_key():
    """`.env.example` ships `sk-ant-xxxxx`, and for a while that placeholder sat
    in the real .env making the narration path look configured while every call
    silently fell back to a template."""
    assert not P._usable("sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx")
    assert not P._usable("")
    assert not P._usable("short")
    assert P._usable("AIzaSyD" + "a" * 30)


def test_no_provider_raises_rather_than_returning_a_no_op():
    """A system that quietly stops understanding replies looks identical to one
    where no customer ever replied."""
    with pytest.raises(P.LLMUnavailable) as e:
        P.get_provider(env={})
    assert "GEMINI_API_KEY" in str(e.value)


def test_gemini_is_preferred_when_both_are_configured():
    env = {"GEMINI_API_KEY": "AIza" + "x1" * 20,
           "ANTHROPIC_API_KEY": "sk-ant-" + "a" * 100}
    assert P.get_provider(env=env).name == "gemini"
    assert P.get_provider(env=env, prefer="anthropic").name == "anthropic"


def test_a_fenced_json_block_is_unwrapped_but_nothing_else_is_repaired():
    """Models sometimes fence JSON even when asked not to. Anything beyond that
    one wrapper is a response we would have to guess at, and a response we have
    to guess at is one we should not act on."""
    assert P._parse('```json\n{"a": 1}\n```') == {"a": 1}
    assert P._parse('{"a": 1}') == {"a": 1}
    with pytest.raises(P.LLMError):
        P._parse("sure! the date is Friday")
    with pytest.raises(P.LLMError):
        P._parse('[1,2,3]')


def test_the_response_shape_is_the_one_the_live_api_actually_returns():
    """This test used to assert a shape invented from a doc summary.

    The summary was wrong about the endpoint too, and both errors survived a
    green test suite for the same reason: a test written from the same guess as
    the code confirms the guess. The shape below was read off a real response.
    """
    assert P._gemini_text(
        {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}) == "hi"
    # Multi-part responses concatenate; one part is the common case, not the
    # guaranteed one.
    assert P._gemini_text({"candidates": [{"content": {"parts": [
        {"text": "he"}, {"text": "llo"}]}}]}) == "hello"
    with pytest.raises(P.LLMError):
        P._gemini_text({"nothing": True})


def test_a_truncated_or_blocked_answer_is_an_error_not_an_empty_reading():
    """A candidate with no text means the model stopped for a reason. Reporting
    that as "no text" would hide a safety block behind a parse failure."""
    with pytest.raises(P.LLMError, match="MAX_TOKENS"):
        P._gemini_text({"candidates": [{"content": {"parts": []},
                                        "finishReason": "MAX_TOKENS"}]})
    with pytest.raises(P.LLMError, match="SAFETY"):
        P._gemini_text({"promptFeedback": {"blockReason": "SAFETY"}})


def test_an_empty_env_means_no_credentials_not_go_and_find_some():
    """`env or load_env()` read the process environment whenever a caller
    passed `{}` to mean "no credentials". A test meant to run in isolation
    started hitting the live key the moment one was added to .env."""
    assert P.available(env={}) == []
    assert not P.Gemini(env={}).ready()


# ---------------------------------------------------------------------------
# reading a reply
# ---------------------------------------------------------------------------

def test_a_clear_promise_is_read_with_a_date():
    r = R.read_reply("Sorry, cash is tight. I'll pay on Friday.", NOW,
                     _stub(intent=R.PROMISE, pay_by="2026-09-04",
                           confidence=0.93, quote="I'll pay on Friday",
                           reasoning="explicit commitment to a day"))
    assert r.intent == R.PROMISE
    assert r.pay_by.date().isoformat() == "2026-09-04"
    assert r.actionable


def test_an_opt_out_is_read():
    r = R.read_reply("stop messaging me", NOW,
                     _stub(intent=R.OPT_OUT, confidence=0.98,
                           quote="stop messaging me", reasoning="explicit"))
    assert r.intent == R.OPT_OUT and r.actionable


def test_a_promise_with_no_usable_date_is_not_actionable():
    """A promise that cannot pause anything is not a promise the engine can
    act on -- but it is still worth a person's attention."""
    r = R.read_reply("I'll pay soon", NOW,
                     _stub(intent=R.PROMISE, pay_by=None, confidence=0.9,
                           quote="soon", reasoning="no date given"))
    assert r.intent == R.UNCLEAR
    assert r.needs_human


def test_an_unparseable_date_downgrades_rather_than_crashing():
    r = R.read_reply("next Friday", NOW,
                     _stub(intent=R.PROMISE, pay_by="sometime next week",
                           confidence=0.9, quote="next Friday", reasoning="x"))
    assert r.pay_by is None
    assert r.needs_human


def test_low_confidence_goes_to_a_person(paths):
    """The expensive mistake is not misreading a date -- the horizon check
    catches an absurd one. It is treating 'I already paid' as a promise and
    pausing collection for a week."""
    r = R.read_reply("hmm ok", NOW,
                     _stub(intent=R.PROMISE, pay_by="2026-09-04",
                           confidence=0.4, quote="ok", reasoning="ambiguous"))
    assert r.needs_human
    assert "only 40% sure" in r.why()


@pytest.mark.parametrize("intent", [R.PAID, R.DISPUTE, R.QUESTION, R.UNCLEAR])
def test_everything_that_is_not_a_promise_or_opt_out_needs_a_person(intent):
    r = R.read_reply("...", NOW,
                     _stub(intent=intent, confidence=0.99, quote="x",
                           reasoning="y"))
    assert r.needs_human
    assert r.why()


def test_a_missing_confidence_is_treated_as_no_confidence():
    r = R.read_reply("x", NOW, _stub(intent=R.OPT_OUT, quote="x", reasoning="y"))
    assert r.confidence == 0.0 and r.needs_human


# ---------------------------------------------------------------------------
# the boundary: the model proposes, the engine decides
# ---------------------------------------------------------------------------

def test_a_promise_still_goes_through_the_same_validation(paths):
    from src.recovery import promises
    r = R.read_reply("Friday", NOW,
                     _stub(intent=R.PROMISE, pay_by="2026-09-04",
                           confidence=0.95, quote="Friday", reasoning="x"))
    got = R.apply_reading(r, "inv_1", NOW, _cfg(), **paths)
    assert got.action == "promised"
    assert promises.holds("inv_1", NOW + timedelta(days=1), _cfg(),
                          paths["promise_path"])[0]


def test_a_hallucinated_date_is_refused_by_the_same_rule_a_typo_would_be(paths):
    """The whole safety argument. The horizon ceiling lives in
    `promises.record` and applies identically whether the date came from a model
    or from an operator's keyboard."""
    from src.recovery import promises, review
    r = R.read_reply("I'll pay eventually", NOW,
                     _stub(intent=R.PROMISE, pay_by="2027-06-01",
                           confidence=0.95, quote="eventually", reasoning="x"))
    got = R.apply_reading(r, "inv_2", NOW, _cfg(), **paths)
    assert got.action == "refused"
    assert "limit is 30" in got.detail
    assert not promises.holds("inv_2", NOW, _cfg(), paths["promise_path"])[0]
    assert review.read(paths["review_path"]), "a refusal must reach a person"


def test_a_confident_opt_out_suppresses_the_PERSON_not_the_case(paths):
    """Two different keys, and this test used to conflate them.

    A promise is about a CASE -- this invoice, paused until Friday. An opt-out
    is about a PERSON -- never contact them again, on any case. The original
    version passed an email address as the `reference`, which made it pass
    while the code was suppressing whatever identifier it was handed.
    """
    from src.recovery import suppression
    r = R.read_reply("STOP", NOW,
                     _stub(intent=R.OPT_OUT, confidence=0.99, quote="STOP",
                           reasoning="explicit"))
    got = R.apply_reading(r, "inv_77", NOW, _cfg(),
                          customer_ref="Buyer@Example.com ", **paths)
    assert got.action == "suppressed"
    assert suppression.is_suppressed("buyer@example.com", paths["sup_path"])
    assert not suppression.is_suppressed("inv_77", paths["sup_path"]),         "the invoice id was suppressed instead of the customer"


def test_an_opt_out_with_no_known_contact_goes_to_a_person(paths):
    """Somebody asked us to stop and we do not know who they are. Dropping that
    silently is the worst outcome; suppressing an invoice number is the second
    worst, because it looks like it worked."""
    from src.recovery import review, suppression
    r = R.read_reply("stop", NOW,
                     _stub(intent=R.OPT_OUT, confidence=0.99, quote="stop",
                           reasoning="explicit"))
    got = R.apply_reading(r, "order_X", NOW, _cfg(), customer_ref=None, **paths)
    assert got.action == "escalated"
    assert not suppression.read(paths["sup_path"])
    assert review.read(paths["review_path"])


def test_an_uncertain_reply_never_touches_promises_or_suppression(paths):
    from src.recovery import promises, suppression
    r = R.read_reply("what is this about?", NOW,
                     _stub(intent=R.QUESTION, confidence=0.99, quote="?",
                           reasoning="x"))
    got = R.apply_reading(r, "inv_3", NOW, _cfg(), **paths)
    assert got.action == "escalated"
    assert not promises.read(paths["promise_path"])
    assert not suppression.read(paths["sup_path"])


def test_the_model_is_never_asked_what_to_do_only_what_was_meant():
    """The prompt must not contain the engine's decisions. If a model can see
    the ladder it can be argued into skipping a rung."""
    stub = _stub(intent=R.UNCLEAR, confidence=0.1, quote="", reasoning="")
    R.read_reply("hello", NOW, stub, reference="inv_9", amount_paise=50000)
    sent = stub.calls[0]["prompt"] + stub.calls[0]["system"]
    for forbidden in ("schedule", "ladder", "retry", "quiet hours",
                      "suppress", "floor", "escalate"):
        assert forbidden not in sent.lower(), (
            f"the prompt mentions {forbidden!r}; the model is being shown "
            f"decisions it has no business influencing")


def test_an_unreachable_model_sends_the_reply_to_a_person_not_to_a_traceback(paths):
    """The free tier caps requests per model per day. Being unable to reach a
    model is ordinary, and the reply still exists.

    The failure that costs something is the other one: a customer writes STOP,
    the quota is gone, and the ladder messages them again tomorrow because the
    command exited with a stack trace.
    """
    from src.recovery import promises, review, suppression
    r = R.unreadable("STOP", "the model could not be reached")
    assert r.needs_human and r.confidence == 0.0
    got = R.apply_reading(r, "inv_44", NOW, _cfg(), **paths)
    assert got.action == "escalated"
    assert review.read(paths["review_path"]), "the reply must reach a person"
    assert not suppression.read(paths["sup_path"])
    assert not promises.read(paths["promise_path"])
    assert "nobody read this yet" in r.why()
