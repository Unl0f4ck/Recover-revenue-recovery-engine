"""Tests for the features added from the prior-art review.

Webhooks, the review queue, signed customer links, CSV ingest, reports,
per-merchant config and the gateway contract.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.gateways import get_gateway
from src.gateways.base import Capabilities, available
from src.gateways.razorpay_gateway import BadSignature, RazorpayGateway
from src.ingest.csv_source import read_csv
from src.recovery import dunning as D
from src.recovery import (ledger as L, merchants, reports, review, suppression,
                          tokens, webhooks)
from src.recovery.declines import classify, load_declines

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
T0 = datetime(2026, 8, 28, 12, 0, tzinfo=IST)
SECRET = "whsec_test"


@pytest.fixture
def cfg():
    return load_declines()


@pytest.fixture
def paths(tmp_path):
    return {"path": tmp_path / "ledger.jsonl",
            "sup_path": tmp_path / "suppression.jsonl",
            "notif_path": tmp_path / "notifications.jsonl",
            "review_path": tmp_path / "review.jsonl",
            "wh_path": tmp_path / "webhooks.jsonl",
            "csv": tmp_path / "failures.csv"}


def _signed(body: dict, secret: str = SECRET, event_id: str = "evt_1"):
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Razorpay-Signature": sig, "x-razorpay-event-id": event_id}


def _link_paid(link_id="plink_A", amount=5_00_000):
    return {"event": "payment_link.paid", "created_at": 1787900000,
            "payload": {"payment_link": {"entity": {
                "id": link_id, "amount": amount, "amount_paid": amount}}}}


def _open_seq(cfg, paths, ref="pay_W", link="plink_A", amount=5_00_000):
    seq = D.plan(ref, "payment_failure", amount,
                 classify("otp_attempts_exceeded", cfg), T0, cfg,
                 customer_ref="w@example.com")
    D.open_sequence(seq, paths["path"])
    step = seq.steps[0]
    D.write_ahead(seq, step, T0, paths["path"])
    D.record_outcome(seq, step, D.AttemptOutcome(
        "DELIVERED", "link created", execution_mode="REAL",
        execution_reference=link), T0, paths["path"])
    return seq


# ---------------------------------------------------------------------------
# webhook verification
# ---------------------------------------------------------------------------

def test_signature_is_verified_over_exact_raw_bytes():
    raw, headers = _signed(_link_paid())
    ev = get_gateway("razorpay").verify_webhook(raw, headers, SECRET)
    assert ev.kind == "recovery_paid"
    assert ev.reference == "plink_A"
    assert ev.amount_paise == 5_00_000


def test_a_reserialised_body_is_refused_not_quietly_accepted():
    """`json.dumps(json.loads(body))` is not the same string -- key order,
    whitespace and unicode escaping all differ. A handler that parses before
    verifying rejects every genuine event, and the tempting 'fix' is to loosen
    the check."""
    # The provider's own formatting, which is NOT what json.dumps produces on
    # a round-trip: indentation, separators and key order all differ. Building
    # the fixture with default dumps would make the round-trip byte-identical
    # and the test would pass without exercising anything.
    raw = json.dumps(_link_paid(), indent=2, sort_keys=False).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    headers = {"X-Razorpay-Signature": sig, "x-razorpay-event-id": "evt_r"}
    gw = get_gateway("razorpay")

    assert gw.verify_webhook(raw, headers, SECRET).kind == "recovery_paid"

    with pytest.raises(BadSignature):
        gw.verify_webhook(json.loads(raw), headers, SECRET)      # a dict
    reserialised = json.dumps(json.loads(raw)).encode()
    assert reserialised != raw, "fixture does not exercise a re-serialisation"
    with pytest.raises(BadSignature):
        gw.verify_webhook(reserialised, headers, SECRET)


def test_a_forged_signature_is_rejected():
    raw, _ = _signed(_link_paid())
    with pytest.raises(BadSignature):
        get_gateway("razorpay").verify_webhook(
            raw, {"X-Razorpay-Signature": "0" * 64}, SECRET)


def test_a_missing_signature_header_is_rejected():
    raw, _ = _signed(_link_paid())
    with pytest.raises(BadSignature):
        get_gateway("razorpay").verify_webhook(raw, {}, SECRET)


def test_a_rotated_secret_still_accepts_a_retry():
    """Providers retry, and a retry can arrive after the secret was rotated,
    signed with the OLD one. Without this a rotation silently drops a day."""
    raw, headers = _signed(_link_paid(), secret="old_secret")
    gw = get_gateway("razorpay")
    with pytest.raises(BadSignature):
        gw.verify_webhook(raw, headers, "new_secret")
    ev = gw.verify_webhook(raw, headers, "new_secret",
                           previous_secrets=["old_secret"])
    assert ev.kind == "recovery_paid"


def test_an_unknown_event_is_normalised_not_dropped():
    """medusa#16398 is precisely a handler returning early on an event it does
    not recognise. Unknown becomes `other` and is still recorded."""
    raw, headers = _signed({"event": "invoice.something_new",
                            "created_at": 1787900000, "payload": {}})
    ev = get_gateway("razorpay").verify_webhook(raw, headers, SECRET)
    assert ev.kind == "other"
    assert ev.event == "invoice.something_new"


# ---------------------------------------------------------------------------
# webhook application
# ---------------------------------------------------------------------------

def test_a_paid_link_webhook_records_the_recovery(cfg, paths):
    _open_seq(cfg, paths)
    raw, headers = _signed(_link_paid())
    got = webhooks.handle(raw, headers, SECRET, T0 + timedelta(hours=1),
                          cfg=cfg, path=paths["path"], wh_path=paths["wh_path"])
    assert got.applied == "recovered"
    assert L.has_succeeded("pay_W", paths["path"])
    assert L.is_stopped("pay_W", paths["path"])


def test_a_replayed_webhook_does_not_record_twice(cfg, paths):
    """Providers retry. An applied-twice event records a recovery twice."""
    _open_seq(cfg, paths)
    raw, headers = _signed(_link_paid())
    webhooks.handle(raw, headers, SECRET, T0, cfg=cfg, path=paths["path"],
                    wh_path=paths["wh_path"])
    before = L.recovered_paise(paths["path"])
    again = webhooks.handle(raw, headers, SECRET, T0 + timedelta(minutes=5),
                            cfg=cfg, path=paths["path"],
                            wh_path=paths["wh_path"])
    assert again.duplicate
    assert L.recovered_paise(paths["path"]) == before


def test_a_payment_paid_elsewhere_closes_the_campaign(cfg, paths):
    """medusa#16398: an unobserved terminal state means the campaign keeps
    escalating at someone who has already paid -- by going back to checkout, or
    by support taking payment by hand."""
    _open_seq(cfg, paths, ref="pay_Z", link="plink_Z")
    raw, headers = _signed({"event": "order.paid", "created_at": 1787900000,
                            "payload": {"order": {"entity": {
                                "id": "pay_Z", "amount": 5_00_000}}}},
                           event_id="evt_paid")
    got = webhooks.handle(raw, headers, SECRET, T0 + timedelta(hours=1),
                          cfg=cfg, path=paths["path"], wh_path=paths["wh_path"])
    assert got.applied == "closed"
    assert L.is_stopped("pay_Z", paths["path"])


def test_a_webhook_never_creates_a_contact(cfg, paths):
    """A webhook at 03:00 must not cause an SMS at 03:00. Anything needing an
    outbound step waits for a pass, where quiet hours and the caps apply."""
    raw, headers = _signed({"event": "payment.failed", "created_at": 1787900000,
                            "payload": {"payment": {"entity": {
                                "id": "pay_new", "amount": 9_00_000}}}})
    got = webhooks.handle(raw, headers, SECRET, T0, cfg=cfg,
                          path=paths["path"], wh_path=paths["wh_path"])
    assert got.applied == "queued"
    assert L.read(paths["path"]) == [], "a webhook opened a campaign directly"


# ---------------------------------------------------------------------------
# manual-review queue
# ---------------------------------------------------------------------------

def _escalate(cfg, paths, ref="pay_E", amount=9_00_000):
    seq = D.plan(ref, "payment_failure", amount,
                 classify("insufficient_funds", cfg), T0, cfg)
    D.open_sequence(seq, paths["path"])
    for s in seq.steps[:-1]:
        D.record_outcome(seq, s, D.AttemptOutcome("FAILED", "x"), s.due_at,
                         paths["path"])
    D.stop(seq, D.STOP_TERMINAL_CHANNEL, seq.steps[-1].due_at, path=paths["path"])
    return seq


def test_escalation_reaches_a_queue(cfg, paths):
    """It used to escalate, stop, and then nothing -- a stopping rule wearing a
    handoff's clothes."""
    _escalate(cfg, paths)
    q = review.queue(T0 + timedelta(days=20), cfg, paths["path"],
                     paths["review_path"])
    assert len(q) == 1
    assert q[0].reference == "pay_E"
    assert q[0].state == "open"


def test_a_written_off_campaign_is_not_in_the_queue(cfg, paths):
    """A write-off ended because the ladder said stop. Nobody is waiting."""
    seq = D.plan("order_W", "checkout_abandoned", 9_00_000,
                 classify("insufficient_funds", cfg), T0, cfg)
    from src.recovery.declines import classify_abandonment
    seq = D.plan("order_W", "checkout_abandoned", 9_00_000,
                 classify_abandonment(cfg), T0, cfg)
    D.open_sequence(seq, paths["path"])
    for s in seq.steps[:-1]:
        D.record_outcome(seq, s, D.AttemptOutcome("FAILED", "x"), s.due_at,
                         paths["path"])
    D.stop(seq, D.STOP_TERMINAL_CHANNEL, seq.steps[-1].due_at, path=paths["path"])
    assert review.queue(T0 + timedelta(days=20), cfg, paths["path"],
                        paths["review_path"]) == []


def test_claiming_and_resolving_moves_an_item(cfg, paths):
    _escalate(cfg, paths)
    now = T0 + timedelta(days=20)
    review.claim("pay_E", "ops-jo", now, path=paths["review_path"])
    q = review.queue(now, cfg, paths["path"], paths["review_path"])
    assert q[0].state == "claimed" and q[0].claimed_by == "ops-jo"

    review.resolve("pay_E", "ops-jo", now, "called the customer; card replaced",
                   path=paths["review_path"])
    assert review.queue(now, cfg, paths["path"], paths["review_path"]) == []


def test_resolving_requires_a_note(cfg, paths):
    """An escalation closed without a reason tells the next person nothing, and
    capturing the judgement was the whole point of routing it to a human."""
    _escalate(cfg, paths)
    with pytest.raises(ValueError):
        review.resolve("pay_E", "ops-jo", T0, "", path=paths["review_path"])
    with pytest.raises(ValueError):
        review.resolve("pay_E", "", T0, "done", path=paths["review_path"])


def test_the_queue_ages(cfg, paths):
    _escalate(cfg, paths)
    now = T0 + timedelta(days=30)
    item = review.queue(now, cfg, paths["path"], paths["review_path"])[0]
    assert item.ageing_band(now) == "over a week"
    s = review.summary(now, cfg, paths["path"], paths["review_path"])
    assert s["open"] == 1 and s["oldest_days"] >= 7


# ---------------------------------------------------------------------------
# signed customer links
# ---------------------------------------------------------------------------

def test_a_token_round_trips():
    now = datetime.now(UTC)
    t = tokens.issue(tokens.OPT_OUT, "a@example.com", "s3cret", now)
    got = tokens.verify(t, "s3cret", now)
    assert got.purpose == tokens.OPT_OUT
    assert got.customer_ref == "a@example.com"


def test_a_token_cannot_be_edited_into_another_purpose():
    """The purpose is INSIDE the signed payload. An instrument-update link must
    not be replayable as an opt-out."""
    now = datetime.now(UTC)
    t = tokens.issue(tokens.UPDATE_INSTRUMENT, "a@example.com", "s3cret", now)
    body, sig = t.split(".", 1)
    import base64
    d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    d["p"] = tokens.OPT_OUT
    forged = base64.urlsafe_b64encode(
        json.dumps(d, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    with pytest.raises(tokens.BadToken):
        tokens.verify(f"{forged}.{sig}", "s3cret", now)


def test_a_token_from_another_secret_is_rejected():
    now = datetime.now(UTC)
    t = tokens.issue(tokens.OPT_OUT, "a@example.com", "one", now)
    with pytest.raises(tokens.BadToken):
        tokens.verify(t, "two", now)


def test_an_expired_token_is_rejected():
    now = datetime.now(UTC)
    t = tokens.issue(tokens.OPT_OUT, "a@example.com", "s", now,
                     ttl=timedelta(seconds=1))
    with pytest.raises(tokens.BadToken):
        tokens.verify(t, "s", now + timedelta(minutes=1))


def test_clicking_the_opt_out_link_suppresses_the_customer(paths):
    """This is what makes the suppression list reachable by the person it is
    for, rather than only by an operator who happened to read a reply."""
    now = datetime.now(UTC)
    t = tokens.issue(tokens.OPT_OUT, "Click@Example.com", "s", now)
    who = tokens.redeem_opt_out(t, "s", now, path=paths["sup_path"])
    assert who == "Click@Example.com"
    assert suppression.is_suppressed("click@example.com", paths["sup_path"])


def test_an_update_token_cannot_be_redeemed_as_an_opt_out(paths):
    now = datetime.now(UTC)
    t = tokens.issue(tokens.UPDATE_INSTRUMENT, "a@example.com", "s", now)
    with pytest.raises(tokens.BadToken):
        tokens.redeem_opt_out(t, "s", now, path=paths["sup_path"])


def test_refuses_to_sign_with_an_empty_secret():
    with pytest.raises(ValueError):
        tokens.issue(tokens.OPT_OUT, "a@example.com", "", datetime.now(UTC))


# ---------------------------------------------------------------------------
# CSV ingest
# ---------------------------------------------------------------------------

def test_csv_reads_a_merchant_export(paths):
    paths["csv"].write_text(
        "Payment ID,Amount,Failure Reason,Email,Date,Bank\n"
        "pay_1,45000,insufficient_funds,a@example.com,2026-08-20,HDFC\n"
        "pay_2,120000,card_expired,b@example.com,2026-08-21,ICICI\n",
        encoding="utf-8")
    res = read_csv(paths["csv"], amount_unit="paise")
    assert len(res.items) == 2 and not res.rejected
    assert res.items[0].reference == "pay_1"
    assert res.items[0].detail["error_reason"] == "insufficient_funds"
    assert res.items[0].detail["customer_ref"] == "a@example.com"
    assert res.at_risk_paise == 165000


def test_csv_amount_unit_is_explicit_never_guessed(paths):
    """A genuine Rs 45,000 failure and a 45,000-paise one look identical.
    Guessing from magnitude gets it wrong on exactly the rows that matter, and a
    hundredfold error in exposure drives every gate the wrong way."""
    paths["csv"].write_text("id,amount\npay_1,450\n", encoding="utf-8")
    assert read_csv(paths["csv"], "paise").items[0].amount_paise == 450
    assert read_csv(paths["csv"], "rupees").items[0].amount_paise == 45000
    with pytest.raises(ValueError):
        read_csv(paths["csv"], "dollars")


def test_a_bad_csv_row_is_reported_not_skipped(paths):
    """A silent drop is how a merchant concludes recovery does not work for
    them, when in fact a third of their failures never entered the system."""
    paths["csv"].write_text(
        "id,amount,date\n"
        "pay_1,45000,2026-08-20\n"
        "pay_2,not-a-number,2026-08-20\n"
        ",900,2026-08-20\n",
        encoding="utf-8")
    res = read_csv(paths["csv"])
    assert len(res.items) == 1
    assert len(res.rejected) == 2
    assert all("line" in r and "error" in r for r in res.rejected)


def test_csv_items_flow_through_the_normal_classifier(cfg, paths):
    paths["csv"].write_text(
        "id,amount,reason\npay_1,900000,card_expired\n", encoding="utf-8")
    from src.recovery.campaign import classify_item
    item = read_csv(paths["csv"]).items[0]
    assert classify_item(item, cfg).decline_class == "HARD_INSTRUMENT"


# ---------------------------------------------------------------------------
# reports and analytics
# ---------------------------------------------------------------------------

def test_daily_report_counts_the_day(cfg, paths):
    _open_seq(cfg, paths)
    rep = reports.daily(T0 + timedelta(hours=2), cfg=cfg, path=paths["path"],
                        notif_path=paths["notif_path"],
                        review_path=paths["review_path"])
    assert rep.opened == 1
    assert rep.contacts == 1
    assert rep.recovered_count == 0


def test_analytics_flags_a_thin_sample(cfg, paths):
    """A 100% recovery rate over one campaign is not a recovery rate, and
    printing it without this line is how a demo number becomes a claim."""
    _open_seq(cfg, paths)
    rows = reports.by_decline_class(T0 + timedelta(hours=1), cfg, paths["path"])
    assert rows and rows[0].sample_warning()
    assert "too few" in rows[0].sample_warning()


def test_by_attempt_shows_which_rung_recovers(cfg, paths):
    """The most actionable number a dunning system produces: if attempt 3 never
    recovers anything, the ladder is one rung too long."""
    _open_seq(cfg, paths)
    rows = reports.by_attempt(T0 + timedelta(hours=1), paths["path"])
    assert rows[0]["attempt"] == 1
    assert rows[0]["contacts"] == 1
    assert rows[0]["thin"] is True


def test_drift_detection_finds_a_link_paid_that_we_missed(cfg, paths):
    """The one thing reconciliation is for. Everything else in the report comes
    from our own records; only this can catch a link paid while our ledger still
    says delivered."""
    _open_seq(cfg, paths)

    class PaidGateway:
        def fetch_recovery(self, ref):
            from src.gateways.base import RecoveryArtefact
            return RecoveryArtefact(gateway="razorpay", reference=ref, url=None,
                                    amount_paise=5_00_000, status="paid",
                                    amount_paid_paise=5_00_000)

    drift = reports.find_drift(cfg, paths["path"], PaidGateway())
    assert len(drift) == 1
    assert drift[0].ledger_says == "not recovered"
    assert drift[0].gateway_says == "paid"


# ---------------------------------------------------------------------------
# gateway contract
# ---------------------------------------------------------------------------

def test_capabilities_state_what_we_cannot_do():
    """Every honesty caveat in this project is a capability gap. Stating them as
    data is what stops a Razorpay limitation being hard-coded as a universal
    truth about payments."""
    caps = get_gateway("razorpay").capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.can_restrict_methods is False
    assert caps.can_charge_saved_instrument is False
    assert any("restrict" in line for line in caps.explain())
    assert any("without the customer" in line for line in caps.explain())


def test_the_registry_resolves_and_rejects():
    assert "razorpay" in available()
    assert isinstance(get_gateway("razorpay"), RazorpayGateway)
    with pytest.raises(KeyError):
        get_gateway("stripe")


# ---------------------------------------------------------------------------
# per-merchant configuration
# ---------------------------------------------------------------------------

def test_default_merchant_is_the_plain_config(cfg):
    m = merchants.get("default")
    assert m.cfg["stopping"]["max_attempts"] == cfg["stopping"]["max_attempts"]
    assert m.ledger.name == "dunning_ledger.jsonl"


def test_a_merchant_config_is_a_diff_not_a_copy(cfg):
    """Copying the whole config per merchant rots: the default changes, the
    copies do not, and a bound quietly stops matching the README."""
    m = merchants.get("acme_subs")
    assert m.cfg["stopping"]["max_attempts"] == 8            # overridden
    assert m.cfg["compliance"]["quiet_hours_ist"] == \
        cfg["compliance"]["quiet_hours_ist"]                 # inherited
    assert m.cfg["schedules"].keys() == cfg["schedules"].keys()


def test_merchant_ledgers_do_not_collide():
    a, b = merchants.get("default"), merchants.get("acme_subs")
    assert a.ledger != b.ledger
    assert a.suppression != b.suppression
    assert set(a.paths()) == {"path", "sup_path", "notif_path"}


def test_an_unknown_merchant_is_an_error_not_a_silent_default():
    with pytest.raises(KeyError):
        merchants.get("no_such_merchant")
