"""Regression evidence for the final boundary-by-boundary audit."""
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.recovery import campaign as C, dunning as D, ledger as L, service, suppression, channels
from src.recovery.declines import load_declines
from src.gateways.base import RecoveryArtefact, Capabilities
from src.ingest.razorpay_source import RevenueAtRisk

NOW = datetime(2026, 9, 5, 12, tzinfo=C.IST)
CFG = load_declines()
ITEM = RevenueAtRisk("checkout_abandoned", "order_audit", NOW, 1000000, "TEST", "card", {"customer_ref": "audit@example.com"})


class Gateway:
    def __init__(self):
        self.writes = []
        self.status = "created"

    def capabilities(self):
        return Capabilities(name="fake")

    def fetch_recovery(self, ref):
        return RecoveryArtefact(gateway="fake", reference=ref, url="https://example.invalid/test", amount_paise=ITEM.amount_paise, mode="REAL", status=self.status)

    def cancel_recovery(self, ref):
        self.writes.append(("cancel", ref))
        self.status = "cancelled"

    def create_recovery(self, *args, **kw):
        self.writes.append(("create", kw))
        return replace(self.fetch_recovery("plink_new"), status="created")


@pytest.fixture
def book(tmp_path, monkeypatch):
    paths = dict(path=tmp_path/"ledger.jsonl", sup_path=tmp_path/"sup.jsonl", notif_path=tmp_path/"notify.jsonl", promise_path=tmp_path/"promise.jsonl")
    C.open_new([ITEM], NOW-timedelta(days=2), CFG, C.CampaignResult(NOW), paths["path"])
    seq = C.rebuild(ITEM.reference, CFG, paths["path"])
    step = seq.steps[0]
    D.write_ahead(seq, step, NOW-timedelta(days=1), paths["path"])
    D.record_outcome(seq, step, D.AttemptOutcome("DELIVERED", "created", execution_mode="REAL", execution_reference="plink_old"), NOW-timedelta(days=1), paths["path"])
    monkeypatch.setattr(service, "observe", lambda *a, **kw: "created")
    return paths, Gateway()


@pytest.mark.parametrize("hold", ["budget", "quiet", "optout", "kill", "unknown"])
def test_held_pass_does_not_cancel_current_link(book, monkeypatch, hold):
    paths, gw = book
    extra = {}
    now = NOW
    if hold == "budget": extra["limit"] = 0
    if hold == "quiet": now = NOW.replace(hour=23)
    if hold == "optout": suppression.suppress(ITEM.detail["customer_ref"], NOW, path=paths["sup_path"])
    if hold == "kill": extra["kill_switch"] = True
    if hold == "unknown": monkeypatch.setattr(service, "observe", lambda *a, **kw: "unknown")
    service.live_pass([], now, CFG, gateway=gw, env={"test": "true"}, **paths, **extra)
    assert gw.writes == []


def test_allowed_replacement_cancels_only_at_create_boundary(book):
    paths, gw = book
    result = service.live_pass([], NOW, CFG, gateway=gw, env={"test": "true"}, **paths)
    assert [op for op, _ in gw.writes] == ["cancel", "create"]
    assert result.advanced


def test_webhook_closed_sequence_still_retires_payable_link(book):
    paths, gw = book
    seq = C.rebuild(ITEM.reference, CFG, paths["path"])
    D.stop(seq, D.STOP_TERMINAL_STATE, NOW, path=paths["path"])
    service.live_pass([], NOW, CFG, gateway=gw, env={"test": "true"}, **paths)
    assert gw.writes == [("cancel", "plink_old")]


def test_settled_case_closes_even_when_intervention_budget_is_zero(book, monkeypatch):
    paths, gw = book
    monkeypatch.setattr(service, "observe", lambda *a, **kw: "paid")
    service.live_pass([], NOW, CFG, gateway=gw, env={"test": "true"}, limit=0, **paths)
    assert L.is_stopped(ITEM.reference, paths["path"])
    assert gw.writes == [("cancel", "plink_old")]


def test_csv_rejects_invalid_and_duplicate_money_rows(tmp_path):
    from src.ingest.csv_source import read_csv
    f = tmp_path/"input.csv"
    f.write_text("reference,amount,kind\norder_valid,1.01,payment_failure\norder_valid,2,payment_failure\norder_nan,NaN,payment_failure\norder_fraction,0.001,payment_failure\norder_negative,-3,payment_failure\norder_unknown,100,typo\n", encoding="utf-8")
    r = read_csv(f, "rupees")
    assert len(r.items) == 1 and r.items[0].amount_paise == 101
    assert len(r.rejected) == 5


def test_changed_balance_is_held_before_recovery(monkeypatch):
    from src.execution import razorpay as rz
    monkeypatch.setattr(rz, "_call", lambda *a, **kw: {"status": "attempted", "amount": 1000000, "amount_paid": 500000})
    with pytest.raises(ValueError, match="amount changed"):
        service.observe("order_changed", {"test": "true"}, expected_amount=1000000)


def test_one_obligation_is_not_failed_payment_plus_abandoned_order(monkeypatch):
    from src.ingest.razorpay_source import RealPayment, payment_failures, abandoned_checkouts
    p = RealPayment("pay_test", NOW-timedelta(days=1), 1000000, "failed", "card", None, None, None, None, None, None, "insufficient_funds", "order_test")
    order = {"id": "order_test", "amount": 1000000, "status": "created", "created_at": int(p.created_at.timestamp())}
    assert len(payment_failures([p])) == 1
    assert abandoned_checkouts([order], [], [p], now=NOW) == []
    assert payment_failures([p], {"order_test"}) == []
    assert payment_failures([p, replace(p, payment_id="pay_refunded", status="refunded")]) == []


def test_abandoned_link_retains_contact_identity():
    from src.ingest.razorpay_source import abandoned_checkouts
    link = {"id": "plink_customer", "order_id": "order_link", "amount": 1000000,
            "status": "created", "created_at": int((NOW-timedelta(days=1)).timestamp()),
            "customer": {"email": "customer@example.com"}}
    items = abandoned_checkouts([], [link], [], now=NOW)
    assert items[0].detail["customer_ref"] == "customer@example.com"


def test_placeholder_webhook_secret_is_not_a_live_capability(tmp_path):
    from fastapi.testclient import TestClient
    from src.web.app import create_app
    app = create_app(tmp_path, {"RAZORPAY_WEBHOOK_SECRET": "replace-with-xxxx-secret"})
    with TestClient(app, headers={"X-Recovery-Client": "console"}) as client:
        assert client.get("/api/status").json()["webhook"] is False
        assert client.post("/webhooks/razorpay", content=b"{}").status_code == 503
    app.state.pool.shutdown(wait=True)
