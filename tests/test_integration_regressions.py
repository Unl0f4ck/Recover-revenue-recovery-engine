"""Exercise real boundaries: command -> ledger, gateway transport, HTTP -> engine."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import hashlib
import hmac
import json
import sys
import urllib.error

import pytest
from fastapi.testclient import TestClient

from src.execution import razorpay as rz
from src.gateways.razorpay_gateway import _reference_of
from src.ingest.razorpay_source import RealPayment, RevenueAtRisk, payment_failures
from src.recovery import campaign as C, dunning as D, ledger as L, merchants, runlock, suppression, tokens
from src.recovery.declines import load_declines
from src.recovery.operations import PendingOperation

NOW = datetime(2026, 9, 5, 12, tzinfo=C.IST)
CFG = load_declines()
ITEM = RevenueAtRisk("checkout_abandoned", "order_regression", NOW, 1000000, "TEST", "card", {"customer_ref": "regression@example.com"})


@pytest.fixture
def paths(tmp_path):
    return {"path": tmp_path/"ledger.jsonl", "notif_path": tmp_path/"notifications.jsonl",
            "sup_path": tmp_path/"suppression.jsonl", "promise_path": tmp_path/"promises.jsonl"}


def test_transport_never_blindly_repeats_a_post(monkeypatch):
    calls = []
    def fail(req, timeout):
        calls.append(req)
        raise urllib.error.URLError("response lost after commit")
    monkeypatch.setattr(rz.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError):
        rz._call("POST", "/payment_links", {}, {"RAZORPAY_KEY_ID": "rzp_test_fake", "RAZORPAY_KEY_SECRET": "fake"})
    assert len(calls) == 1


def test_lost_create_is_looked_up_after_process_restart(tmp_path, monkeypatch):
    calls = []
    key = "rcv_regression"
    def call(method, url, body, env):
        calls.append((method, body))
        if method == "POST":
            assert body["reference_id"] == key
            raise RuntimeError("response lost")
        return {"payment_links": [{"id": "plink_created", "reference_id": key, "short_url": "https://example.invalid"}]}
    monkeypatch.setattr(rz, "_call", call)
    env = {"RECOVERY_OPERATIONS_DB": str(tmp_path/"operations.db")}
    with pytest.raises(RuntimeError):
        rz.create_recovery_link(100000, [], "case", env, notes={"idempotency_key": key})
    result = rz.create_recovery_link(100000, [], "case", env, notes={"idempotency_key": key})
    assert result.reference == "plink_created"
    rz.create_recovery_link(100000, [], "case", env, notes={"idempotency_key": key})
    assert [c[0] for c in calls] == ["POST", "GET"]
    with pytest.raises(ValueError):
        rz.create_recovery_link(200000, [], "case", env, notes={"idempotency_key": key})


def test_unresolved_create_is_held_not_reissued(tmp_path, monkeypatch):
    calls = []
    def call(method, url, body, env):
        calls.append(method)
        if method == "POST":
            raise RuntimeError("uncertain")
        return {"payment_links": []}
    monkeypatch.setattr(rz, "_call", call)
    args = (100000, [], "case", {"RECOVERY_OPERATIONS_DB": str(tmp_path/"operations.db")})
    with pytest.raises(RuntimeError):
        rz.create_recovery_link(*args, notes={"idempotency_key": "same"})
    with pytest.raises(PendingOperation):
        rz.create_recovery_link(*args, notes={"idempotency_key": "same"})
    assert calls == ["POST", "GET"]


def test_cap_considers_the_next_obligation(paths):
    C.run_pass([ITEM], NOW, CFG, **paths)
    result = C.run_pass([], NOW+timedelta(hours=2), CFG,
                        policy={"batch_limits": {"max_exposure_per_run_paise": 100000}}, **paths)
    assert result.spent_paise == 0 and not result.advanced
    assert "cap" in result.deferred[0]["reason"]


def test_confirmed_failed_unknown_can_leave_reconciliation(paths):
    item = replace(ITEM, kind="payment_failure", detail={"error_reason": "new-code", "status": "failed"})
    result = C.run_pass([item], NOW, CFG, dry_run=False, **paths)
    assert result.advanced[0]["channel"] == "reconcile"
    assert L.attempts_made(item.reference, paths["path"]) == 1
    assert not L.has_succeeded(item.reference, paths["path"])


def test_campaign_preserves_its_original_plan(paths):
    C.run_pass([ITEM], NOW, CFG, **paths)
    before = C.rebuild(ITEM.reference, CFG, paths["path"])
    import copy
    changed = copy.deepcopy(CFG)
    changed["policy_version"] = "new-policy"
    changed["schedules"]["abandoned"]["steps"][0]["after_hours"] = 500
    after = C.rebuild(ITEM.reference, changed, paths["path"])
    assert before.steps == after.steps
    assert before.policy_version == after.policy_version
    assert before.sequence_id == after.sequence_id


def test_measured_outage_changes_the_first_recovery_step(paths):
    item = replace(ITEM, kind="payment_failure", detail={"error_reason": "bank_technical_error",
                   "diagnosis_family": "issuer_degradation", "status": "failed"})
    C.run_pass([item], NOW, CFG, **paths)
    seq = C.rebuild(item.reference, CFG, paths["path"])
    assert seq.steps[0].channel == "alternate_method_link"
    assert seq.steps[0].due_at == NOW + timedelta(hours=1)
    assert D.exclude_methods(seq, seq.steps[0]) == ["card"]


def test_paid_order_is_not_recovered_from_its_failed_attempts():
    failed = RealPayment("pay_a", NOW, 100000, "failed", "card", None, None, None, None, None, None, "insufficient_funds", "order_a")
    success = replace(failed, payment_id="pay_b", status="captured")
    assert payment_failures([failed, success]) == []
    assert len(payment_failures([failed, replace(failed, payment_id="pay_c")])) == 1


def test_order_webhook_uses_order_identity():
    payload = {"event": "order.paid", "payload": {"payment": {"entity": {"id": "pay_a", "order_id": "order_a"}}, "order": {"entity": {"id": "order_a"}}}}
    assert _reference_of(payload) == "order_a"


def test_live_pass_stops_paid_cases_even_when_absent_from_feed(paths, monkeypatch):
    from src.recovery import service
    from src.gateways.base import Capabilities
    class Gateway:
        def capabilities(self):
            return Capabilities(name="fake")
    C.run_pass([ITEM], NOW, CFG, **paths)
    monkeypatch.setattr(service, "observe", lambda *a, **k: "paid")
    result = service.live_pass([], NOW+timedelta(hours=2), CFG, gateway=Gateway(), env={"test": "true"}, **paths)
    assert L.is_stopped(ITEM.reference, paths["path"])
    assert not result.advanced


def test_live_pass_holds_when_observation_fails(paths, monkeypatch):
    from src.recovery import service
    C.run_pass([ITEM], NOW, CFG, **paths)
    def unavailable(*a, **kw): raise RuntimeError("offline")
    monkeypatch.setattr(service, "observe", unavailable)
    result = service.live_pass([], NOW+timedelta(hours=2), CFG, gateway=object(), env={"test": "true"}, **paths)
    assert not result.advanced
    assert not L.is_stopped(ITEM.reference, paths["path"])
    assert result.deferred


def test_execute_horizon_is_refused_before_io(monkeypatch):
    from scripts import run_dunning as cli
    monkeypatch.setattr(sys, "argv", ["run_dunning", "--execute", "--horizon", "1"])
    with pytest.raises(SystemExit, match="preview"):
        cli.main()


def test_cli_preview_uses_separate_notification_ledger(tmp_path, monkeypatch, capsys):
    from scripts import run_dunning as cli
    monkeypatch.setattr(merchants, "DATA", tmp_path)
    monkeypatch.setattr(cli, "SCRATCH", tmp_path/"scratch.jsonl")
    monkeypatch.setattr(cli, "collect", lambda: [])
    monkeypatch.setattr(runlock, "exclusive", lambda **kw: nullcontext())
    seen = []
    def run(items, now, cfg, **kw):
        seen.append(kw)
        return C.CampaignResult(now)
    monkeypatch.setattr(cli, "run_pass", run)
    monkeypatch.setattr(sys, "argv", ["run_dunning", "--notify"])
    cli.main()
    assert seen[0]["notif_path"] != merchants.get().notifications
    assert seen[0]["dry_run"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.web.app import create_app
    monkeypatch.setattr(merchants, "DATA", tmp_path/"live")
    monkeypatch.setattr(runlock, "LOCK", tmp_path/"live.lock")
    app = create_app(tmp_path/"web", {"RECOVERY_TOKEN_SECRET": "test-secret", "RAZORPAY_WEBHOOK_SECRET": "webhook-secret"})
    with TestClient(app) as client:
        client.headers["X-Recovery-Client"] = "console"
        yield client
    app.state.pool.shutdown(wait=True)


def test_operator_api_requires_same_origin_and_custom_header(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/status", headers={"X-Recovery-Client": ""}).status_code == 403
    assert client.post("/api/batches", headers={"Origin": "https://evil.example"}, json={}).status_code == 403


def test_ledger_cache_reuses_unchanged_parse_and_sees_appends(tmp_path):
    from src.recovery.jsonl import read_jsonl
    path = tmp_path/"rows.jsonl"
    path.write_text('{"n":1}\n', encoding="utf-8")
    first = read_jsonl(path)
    assert read_jsonl(path) is first
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"n":2}\n')
    assert read_jsonl(path) == [{"n": 1}, {"n": 2}]


def test_csv_to_campaign_to_exact_reply_proposal(client):
    csv = "reference,amount,kind,customer_ref\norder_import,10000,checkout_abandoned,person@example.com\n"
    imported = client.post("/api/import", json={"csv": csv, "unit": "rupees"})
    assert imported.status_code == 200
    source = imported.json()["id"]
    snapshot = client.get("/api/snapshot", params={"source": source}).json()
    assert snapshot["mode"] == "CSV PREVIEW"
    assert snapshot["metrics"]["cases"] == 1
    route = f"/api/cases/order_import/reply?source={source}"
    assert client.post(route, json={"text": "STOP", "apply": True}).status_code == 409
    reading = client.post(route, json={"text": "STOP"}).json()
    proposal = reading["proposal_id"]
    assert reading["reading"]["provider"] == "rules"
    assert client.post(route, json={"text": "changed", "apply": True, "proposal_id": proposal}).status_code == 409
    applied = client.post(route, json={"text": "STOP", "apply": True, "proposal_id": proposal})
    assert applied.json()["action"]["action"] == "suppressed"
    assert client.post(route, json={"text": "STOP", "apply": True, "proposal_id": proposal}).json() == applied.json()
    exported = client.get("/api/evidence", params={"source": source})
    assert exported.status_code == 200
    assert exported.headers["x-content-sha256"] == hashlib.sha256(exported.content).hexdigest()


def test_customer_opt_out_requires_post_and_valid_token(client):
    now = datetime.now(C.IST)
    token = tokens.issue(tokens.OPT_OUT, "person@example.com", "test-secret", now)
    assert client.get("/opt-out", params={"t": token}).status_code == 200
    assert not suppression.read(merchants.get().suppression)
    assert client.post("/opt-out", data={"t": token}).status_code == 200
    assert suppression.is_suppressed("person@example.com", merchants.get().suppression)
    assert client.post("/opt-out", data={"t": "forged"}).status_code == 400


def test_instrument_link_is_bound_to_customer_and_closed_campaign(client):
    m = merchants.get()
    C.open_new([ITEM], NOW, CFG, C.CampaignResult(NOW), m.ledger)
    now = datetime.now(C.IST)
    wrong = tokens.issue(tokens.UPDATE_INSTRUMENT, "someone@example.com", "test-secret", now,
                         reference=ITEM.reference)
    assert client.get("/update-instrument", params={"t": wrong}).status_code == 404
    correct = tokens.issue(tokens.UPDATE_INSTRUMENT, ITEM.detail["customer_ref"], "test-secret", now,
                           reference=ITEM.reference)
    seq = C.rebuild(ITEM.reference, CFG, m.ledger)
    D.stop(seq, D.STOP_RECOVERED, now, path=m.ledger)
    response = client.get("/update-instrument", params={"t": correct})
    assert response.status_code == 200
    assert "Campaign closed" in response.text


def test_ai_unavailable_routes_to_review_and_stops_campaign(client, monkeypatch):
    from src.ai import provider
    stub = provider.Stub(fail=True)
    monkeypatch.setattr(provider, "get_provider", lambda *a, **kw: stub)
    csv = "reference,amount,kind,customer_ref\norder_review,10000,checkout_abandoned,person@example.com\n"
    source = client.post("/api/import", json={"csv": csv, "unit": "rupees"}).json()["id"]
    route = f"/api/cases/order_review/reply?source={source}"
    reading = client.post(route, json={"text": "This invoice is wrong"}).json()
    applied = client.post(route, json={"text": "This invoice is wrong", "apply": True,
                                      "proposal_id": reading["proposal_id"]})
    assert applied.json()["action"]["action"] == "escalated"
    assert len(stub.calls) == 1, "applying an existing proposal called the AI again"
    snap = client.get("/api/snapshot", params={"source": source}).json()
    assert snap["campaigns"][0]["state"] == "ESCALATED"
    assert len(snap["review"]) == 1
    assert client.post(f"/api/cases/order_review/resolve?source={source}", json={"note": "Invoice corrected by merchant"}).status_code == 200
    assert client.get("/api/snapshot", params={"source": source}).json()["review"] == []


def test_remote_operator_requires_strong_configured_token(tmp_path):
    from src.web.app import create_app
    app = create_app(tmp_path, {"RECOVERY_ADMIN_TOKEN": "x"*32})
    with TestClient(app) as test:
        assert test.get("/api/status", headers={"X-Recovery-Client": "console"}).status_code == 401
        assert test.get("/api/status", headers={"X-Recovery-Client": "console", "Authorization": "Bearer "+"x"*32}).status_code == 200
    app.state.pool.shutdown()


def test_signed_http_webhook_closes_order_and_deduplicates(client):
    path = merchants.get().ledger
    C.open_new([ITEM], NOW, CFG, C.CampaignResult(NOW), path)
    payload = {"event": "order.paid", "created_at": int(NOW.timestamp()), "payload": {
        "payment": {"entity": {"id": "pay_new", "order_id": ITEM.reference}},
        "order": {"entity": {"id": ITEM.reference, "status": "paid"}}}}
    body = json.dumps(payload).encode()
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/razorpay", content=body).status_code == 400
    first = client.post("/webhooks/razorpay", content=body, headers={"x-razorpay-signature": signature})
    assert first.json()["applied"] == "closed"
    assert L.is_stopped(ITEM.reference, path)
    again = client.post("/webhooks/razorpay", content=body, headers={"x-razorpay-signature": signature})
    assert again.json()["duplicate"]
