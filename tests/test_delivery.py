"""Actually sending the link, and the two locks that stop it going astray.

For most of this project nothing was ever delivered: every Payment Link was
created with `notify: {sms: false, email: false}`, which was the right default
while there was no opt-out list and no delivery ledger. Turning delivery on is
the only thing the sequencer does that reaches a stranger, so the tests here
are almost entirely about who does NOT get messaged.

The hazard is specific and it is not hypothetical. `create_recovery_link` has
to send Razorpay a customer block, so when the caller knows no customer it
substitutes a placeholder -- name, `demo@example.com`, and `+919812345670`.
That last one is a real-format Indian mobile number that belongs to nobody in
this project. Thirty-seven of the live campaigns are Razorpay ORDERS, which
carry no contact details at all. A run-wide `{"sms": True}` would have asked
Razorpay to text that number thirty-seven times.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.execution import razorpay as rz
from src.recovery import channels as CH
from src.recovery import declines, dunning as D
from src.recovery.campaign import delivery_for

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=IST)

BOTH = {"sms": True, "email": True}

# Delivery config with the redirect OFF. The shipped config has it ON so the
# operator can watch the ladder arrive, but the rules below are about ordinary
# behaviour and must be tested against it rather than against a demo setting.
import copy

from src.recovery.channels import load_workflows

NO_REDIRECT = copy.deepcopy(load_workflows())
NO_REDIRECT["delivery"]["redirect"]["enabled"] = False


def _seq(customer_ref):
    cfg = declines.load_declines()
    cls = declines.classify_abandonment(cfg)
    return D.plan("order_x", "checkout_abandoned", 500000, cls, NOW, cfg,
                  customer_ref=customer_ref)


# ---------------------------------------------------------------------------
# who gets messaged, and on what
# ---------------------------------------------------------------------------

def test_a_case_with_no_contact_asks_for_no_delivery():
    """The one that matters, absent a redirect. A Razorpay order carries
    neither e-mail nor phone, so there is nobody to message -- and the
    placeholder the API call needs is not a person who agreed to hear from
    us."""
    assert delivery_for(_seq(None), BOTH, NO_REDIRECT) == {"sms": False,
                                                           "email": False}
    assert delivery_for(_seq("   "), BOTH, NO_REDIRECT) == {"sms": False,
                                                             "email": False}


def test_an_email_address_gets_email_and_not_sms():
    """Asking for SMS on a case where we only hold an address is the same
    mistake in a quieter form: the SMS goes to the placeholder number."""
    assert delivery_for(_seq("a@b.com"), BOTH, NO_REDIRECT) == {"sms": False,
                                                                 "email": True}


def test_a_phone_number_gets_sms_and_not_email():
    assert delivery_for(_seq("+919812345670"), BOTH, NO_REDIRECT) == {
        "sms": True, "email": False}


def test_delivery_is_off_unless_asked_for():
    assert delivery_for(_seq("a@b.com"), None) == {"sms": False, "email": False}
    assert delivery_for(_seq("a@b.com"), {}) == {"sms": False, "email": False}


def test_a_channel_turned_off_in_config_is_not_requested():
    assert delivery_for(_seq("a@b.com"), {"sms": True, "email": False},
                        NO_REDIRECT) == {"sms": False, "email": False}


# ---------------------------------------------------------------------------
# the redirect
# ---------------------------------------------------------------------------

def test_a_redirect_sends_to_the_operator_and_never_to_the_customer():
    """The whole point: real messages, watched arriving, without messaging a
    customer who is not expecting them."""
    from src.recovery.campaign import contact_for
    wf = load_workflows()
    # Read the pool through `channels.delivery`, not from the raw config. The
    # contacts are no longer IN workflows.yaml -- they are personal addresses,
    # so they live in a gitignored local file and are merged in at load.
    r = CH.delivery(wf)["redirect"]
    if not r["enabled"]:
        pytest.skip("no config/redirect.local.yaml on this machine")
    targets = set(r["email"]) | set(r["sms"])
    to, real = contact_for(_seq("stranger@elsewhere.com"), wf)
    assert to in targets
    assert real == "stranger@elsewhere.com", "the case's own contact must survive"


@pytest.mark.skipif(
    not CH.delivery()["redirect"]["enabled"],
    reason="no config/redirect.local.yaml on this machine")
def test_the_redirect_never_loses_who_the_case_was_really_for():
    """A redirect that rewrote the customer would make the audit trail agree
    with the demo instead of with reality."""
    from src.recovery.campaign import contact_for
    seq = _seq("stranger@elsewhere.com")
    contact_for(seq, load_workflows())
    assert seq.customer_ref == "stranger@elsewhere.com"


@pytest.mark.skipif(
    not CH.delivery()["redirect"]["enabled"],
    reason="no config/redirect.local.yaml on this machine")
def test_the_same_case_always_reaches_the_same_person():
    """Deterministic in the reference, so a re-run does not reshuffle who saw
    what and the operator can follow one case to one inbox."""
    from src.recovery.campaign import redirect_target
    wf = load_workflows()
    a = [redirect_target(_seq("x@y.com"), wf) for _ in range(5)]
    assert len(set(a)) == 1


@pytest.mark.skipif(
    not CH.delivery()["redirect"]["enabled"],
    reason="no config/redirect.local.yaml on this machine")
def test_a_redirect_spreads_across_the_configured_contacts():
    """Piling every case on the first address would leave the others untested
    and one inbox unusable."""
    from src.recovery.campaign import redirect_target
    from src.recovery.declines import classify_abandonment, load_declines
    cfg = load_declines()
    wf = load_workflows()
    seen = set()
    for i in range(60):
        seq = D.plan(f"order_{i}", "checkout_abandoned", 500000,
                     classify_abandonment(cfg), NOW, cfg,
                     customer_ref="a@b.com")
        seen.add(redirect_target(seq, wf))
    assert len(seen) >= 2, seen


@pytest.mark.skipif(
    not CH.delivery()["redirect"]["enabled"],
    reason="no config/redirect.local.yaml on this machine")
def test_a_case_with_no_contact_becomes_deliverable_under_a_redirect():
    """Those cases are muted because the placeholder is not a person who
    agreed to hear from us. A redirect target is exactly that person."""
    want = delivery_for(_seq(None), BOTH, load_workflows())
    assert want["sms"] or want["email"]


def test_delivery_ships_disabled():
    """A system that can send should default to not sending."""
    assert CH.delivery()["enabled"] is False


# ---------------------------------------------------------------------------
# the second lock, at the API boundary
# ---------------------------------------------------------------------------

def _capture(monkeypatch):
    sent = {}

    def fake_call(method, path, body, env):
        sent["method"], sent["path"], sent["body"] = method, path, body
        return {"id": "plink_test", "short_url": "https://x.invalid/l"}

    monkeypatch.setattr(rz, "_call", fake_call)
    return sent


def test_the_placeholder_customer_is_never_notified(monkeypatch):
    """`delivery_for` already refuses this, so nothing should reach here asking
    for it. This is the second lock, because it is the last code that runs
    before a stranger's phone rings."""
    sent = _capture(monkeypatch)
    r = rz.create_recovery_link(500000, [], "x", env={"RAZORPAY_KEY_ID": "rzp_test_x",
                                                      "RAZORPAY_KEY_SECRET": "s"},
                                notify=BOTH, customer=None)
    assert sent["body"]["notify"] == {"sms": False, "email": False}
    assert "delivery refused" in r.detail


def test_a_known_customer_is_notified_as_asked(monkeypatch):
    sent = _capture(monkeypatch)
    rz.create_recovery_link(500000, [], "x",
                            env={"RAZORPAY_KEY_ID": "rzp_test_x",
                                 "RAZORPAY_KEY_SECRET": "s"},
                            notify={"sms": False, "email": True},
                            customer={"email": "a@b.com"})
    assert sent["body"]["notify"] == {"sms": False, "email": True}
    assert sent["body"]["customer"]["email"] == "a@b.com"


def test_the_placeholder_number_is_still_never_the_recipient(monkeypatch):
    """Even with delivery off, a link addressed to the placeholder should not
    be addressed to a customer we were given."""
    sent = _capture(monkeypatch)
    rz.create_recovery_link(500000, [], "x",
                            env={"RAZORPAY_KEY_ID": "rzp_test_x",
                                 "RAZORPAY_KEY_SECRET": "s"},
                            notify=None, customer={"contact": "+919000000001"})
    assert sent["body"]["customer"]["contact"] == "+919000000001"
    assert sent["body"]["notify"] == {"sms": False, "email": False}


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

def test_an_email_does_not_cost_the_same_as_an_sms():
    """The old model charged 20p per ladder STEP and called every step an SMS,
    because there was no delivery channel to charge for."""
    assert CH.delivery_cost("email") < CH.delivery_cost("sms")


def test_an_unknown_delivery_channel_costs_nothing_rather_than_guessing():
    assert CH.delivery_cost("carrier_pigeon") == 0
