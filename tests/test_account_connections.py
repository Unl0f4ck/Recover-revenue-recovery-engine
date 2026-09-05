"""Two independent browsers and mocked provider boundaries; never send real messages."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

from fastapi.testclient import TestClient
import pytest

from src.web.app import create_app
from src.web.accounts import COOKIE, Accounts
from src.execution import razorpay as rz
from src.ingest import razorpay_source as source
from src.recovery import campaign as C, dunning as D, ledger as L, merchants, runlock, notify, suppression, promises
from src.recovery.messaging import send_link, SendBlocked

NOW = datetime(2026, 9, 5, 12, tzinfo=C.IST)
HEADERS = {"X-Recovery-Client": "console"}
KEY = {"key_id": "rzp_test_accountA", "key_secret": "synthetic-secret-A", "name": "Account A"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(merchants, "DATA", tmp_path/"default")
    monkeypatch.setattr(runlock, "LOCK", tmp_path/"writer.lock")
    app = create_app(tmp_path/"console", {"RAZORPAY_KEY_ID": "rzp_test_default", "RAZORPAY_KEY_SECRET": "server-secret"})
    yield app
    app.state.pool.shutdown(wait=True)


def connect(client, monkeypatch, **over):
    monkeypatch.setattr(rz, "_call", lambda *a, **k: {"items": []})
    r = client.post("/api/account/connect", json={**KEY, **over})
    assert r.status_code == 200, r.text
    return r


def test_connect_verifies_read_only_and_never_echoes_or_persists_secrets(app, monkeypatch, tmp_path):
    calls = []
    def call(method, path, body, env, **kw):
        calls.append((method, path, env))
        return {"items": []}
    monkeypatch.setattr(rz, "_call", call)
    with TestClient(app, headers=HEADERS) as c:
        r = c.post("/api/account/connect", json=KEY)
        assert r.status_code == 200
        assert calls[0][0:2] == ("GET", "/payments?count=1")
        assert calls[0][2]["RAZORPAY_KEY_ID"] == KEY["key_id"]
        assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
        assert KEY["key_secret"] not in r.text + c.get("/api/status").text
        a = app.state.accounts.get(c.cookies[COOKIE])
        assert "server-secret" not in repr(a) and "server-secret" not in str(a.env)
        assert not list(tmp_path.rglob("*secret*"))
        assert all(KEY["key_secret"] not in f.read_text(errors="ignore") for f in tmp_path.rglob("*") if f.is_file())


def test_bad_keys_and_validation_never_leak_secret(app, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError(KEY["key_secret"])
    monkeypatch.setattr(rz, "_call", fail)
    with TestClient(app, headers=HEADERS) as c:
        for body in ({**KEY, "key_id": "rzp_live_accountA"}, KEY, {**KEY, "key_secret": "S3cr!"}):
            r = c.post("/api/account/connect", json=body)
            assert r.status_code in (400, 422)
            assert body["key_secret"] not in r.text
        assert not app.state.accounts.sessions


def test_two_accounts_cannot_access_each_others_csv_evidence_or_proposals(app, monkeypatch):
    with TestClient(app, headers=HEADERS) as a, TestClient(app, headers=HEADERS) as b:
        connect(a, monkeypatch)
        connect(b, monkeypatch, key_id="rzp_test_accountB", name="Account B")
        csv = "reference,amount,kind,customer_ref\norder_scoped,10000,checkout_abandoned,person@example.com\n"
        job = a.post("/api/import", json={"csv": csv, "unit": "rupees"}).json()
        assert b.get("/api/status").json()["jobs"] == []
        for path in ("/api/snapshot", "/api/evidence", "/api/notifications", "/api/cases/order_scoped"):
            assert b.get(path, params={"source": job["id"]}).status_code == 404
            assert a.get(path, params={"source": job["id"]}).status_code == 200
        proposal = a.post("/api/cases/order_scoped/reply", params={"source": job["id"]}, json={"text": "STOP"}).json()
        assert b.post("/api/cases/order_scoped/reply", params={"source": job["id"]}, json={"text": "STOP", "apply": True, "proposal_id": proposal["proposal_id"]}).status_code == 404
        assert app.state.accounts.get(a.cookies[COOKIE]).ledger != app.state.accounts.get(b.cookies[COOKIE]).ledger


def test_expired_session_never_falls_back_to_server_credentials(app, monkeypatch):
    with TestClient(app, headers=HEADERS) as c:
        connect(c, monkeypatch)
        sid = c.cookies[COOKIE]
        app.state.accounts.sessions[sid].expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert c.get("/api/snapshot").status_code == 401
        assert sid not in app.state.accounts.sessions
        connect(c, monkeypatch)
        assert c.delete("/api/account").status_code == 200
        assert not app.state.accounts.sessions


def test_link_order_lookup_uses_connected_account(monkeypatch):
    calls = []
    def call(method, path, body, env):
        calls.append(env)
        return {"order_id": "order_backing"}
    monkeypatch.setattr(source, "_call", call)
    source.abandoned_checkouts([], [{"id": "plink_example", "status": "cancelled"}], [], env={"account": "B"})
    assert calls == [{"account": "B"}]


def test_live_execution_passes_notification_choices_and_scoped_paths(app, monkeypatch):
    from src.recovery import runner, service
    from types import SimpleNamespace
    seen = []
    with TestClient(app, headers=HEADERS) as c:
        connect(c, monkeypatch)
        monkeypatch.setattr(runner, "collect", lambda **kw: seen.append(kw) or [])
        monkeypatch.setattr(source, "fetch_payments", lambda **kw: [])
        monkeypatch.setattr(service, "diagnose_items", lambda *a: ([], SimpleNamespace(conclusion="no data")))
        monkeypatch.setattr(service, "live_pass", lambda items, now, cfg, **kw: seen.append(kw) or C.CampaignResult(now))
        assert c.post("/api/live/run", json={"execute": True, "email": True}).status_code == 422
        assert not seen
        r = c.post("/api/live/run", json={"execute": True, "email": True, "sms": True, "confirm_contact": True})
        assert r.status_code == 200
        assert seen[-1]["deliver"] == {"email": True, "sms": True}
        assert seen[-1]["wf_cfg"]["delivery"]["redirect"]["enabled"] is False
        assert seen[-1]["env"]["RAZORPAY_KEY_ID"] == KEY["key_id"]
        assert "accounts" in str(seen[-1]["path"])


@pytest.fixture
def message_case(app, monkeypatch):
    store = app.state.accounts
    monkeypatch.setattr(rz, "_call", lambda *a, **k: {})
    _, account = store.connect(KEY["key_id"], KEY["key_secret"], "A")
    item = source.RevenueAtRisk("checkout_abandoned", "order_message", NOW, 1000000, "TEST", "card", {"customer_ref": "person@example.com"})
    C.open_new([item], NOW-timedelta(days=1), account.cfg, C.CampaignResult(NOW), account.ledger)
    seq = C.rebuild(item.reference, account.cfg, account.ledger)
    step = D.Step(0, NOW-timedelta(hours=5), "payment_link", "same")
    D.write_ahead(seq, step, NOW-timedelta(hours=5), account.ledger)
    D.record_outcome(seq, step, D.AttemptOutcome("DELIVERED", "test link", execution_mode="REAL", execution_reference="plink_example"), NOW-timedelta(hours=5), account.ledger)
    return account, item.reference


def provider_stub(monkeypatch, fail=False, state="created", recipient="person@example.com"):
    calls = []
    def call(method, path, body, env, **kw):
        calls.append((method, path))
        if method == "POST" and fail:
            raise RuntimeError("lost response")
        return {"status": state, "amount": 1000000, "customer": {"email": recipient}}
    monkeypatch.setattr(rz, "_call", call)
    return calls


def test_notification_sent_once_and_never_claims_delivery(message_case, monkeypatch):
    m, ref = message_case
    calls = provider_stub(monkeypatch)
    result = send_link(ref, "email", NOW, m, m.env)
    assert result["status"] == "requested"
    again = send_link(ref, "email", NOW, m, m.env)
    assert again["duplicate"]
    assert [p for method, p in calls if method == "POST"] == ["/payment_links/plink_example/notify_by/email"]
    assert notify.latest_status(ref, 0, "email", m.notifications) == "requested"


def test_uncertain_send_is_held_across_retry(message_case, monkeypatch):
    m, ref = message_case
    calls = provider_stub(monkeypatch, fail=True)
    assert send_link(ref, "email", NOW, m, m.env)["status"] == "unknown"
    assert send_link(ref, "email", NOW+timedelta(hours=6), m, m.env)["duplicate"]
    assert len([x for x in calls if x[0] == "POST"]) == 1


def test_sms_notification_uses_known_phone_and_stable_attempt(app, monkeypatch):
    monkeypatch.setattr(rz, "_call", lambda *a, **k: {})
    _, m = app.state.accounts.connect(KEY["key_id"], KEY["key_secret"], "A")
    phone = "+919000000001"
    item = source.RevenueAtRisk("checkout_abandoned", "order_sms", NOW, 1000000, "TEST", "card", {"customer_ref": phone})
    C.open_new([item], NOW-timedelta(days=1), m.cfg, C.CampaignResult(NOW), m.ledger)
    seq = C.rebuild(item.reference, m.cfg, m.ledger)
    step = D.Step(0, NOW-timedelta(hours=5), "payment_link", "same")
    D.write_ahead(seq, step, step.due_at, m.ledger)
    D.record_outcome(seq, step, D.AttemptOutcome("DELIVERED", "link", execution_reference="plink_sms"), step.due_at, m.ledger)
    calls = []
    def call(method, path, body, env):
        calls.append((method, path))
        return {"status": "created", "amount": 1000000, "customer": {"contact": phone}}
    monkeypatch.setattr(rz, "_call", call)
    assert send_link(item.reference, "sms", NOW, m, m.env)["status"] == "requested"
    assert calls[-1] == ("POST", "/payment_links/plink_sms/notify_by/sms")


def test_http_send_requires_confirmation_and_live_case(app, monkeypatch):
    with TestClient(app, headers=HEADERS) as c:
        connect(c, monkeypatch)
        assert c.post("/api/cases/order_missing/notify", json={"channel": "email"}).status_code == 422
        assert c.post("/api/cases/order_missing/notify", json={"channel": "email", "confirm_contact": True}).status_code == 409
        assert c.post("/api/cases/order_missing/notify", json={"channel": "whatsapp", "confirm_contact": True}).status_code == 422


def test_manual_message_defers_next_automated_contact(message_case, monkeypatch):
    m, ref = message_case
    provider_stub(monkeypatch)
    send_link(ref, "email", NOW, m, m.env)
    seq = C.rebuild(ref, m.cfg, m.ledger)
    decision = D.authorize_contact(seq, D.Step(1, NOW, "payment_link", "same"), NOW+timedelta(minutes=1),
                                   m.cfg, m.ledger, m.suppression, m.notifications)
    assert not decision.allowed
    assert decision.defer_until == NOW+timedelta(hours=4)


def test_preview_with_notifications_enabled_has_no_provider_writes(app, monkeypatch):
    from src.recovery import runner, service
    from types import SimpleNamespace
    calls = []
    with TestClient(app, headers=HEADERS) as c:
        connect(c, monkeypatch)
        monkeypatch.setattr(rz, "_call", lambda method, *a, **k: calls.append(method) or {})
        monkeypatch.setattr(runner, "collect", lambda **kw: [])
        monkeypatch.setattr(source, "fetch_payments", lambda **kw: [])
        monkeypatch.setattr(service, "diagnose_items", lambda *a: ([], SimpleNamespace(conclusion="no data")))
        result = c.post("/api/live/run", json={"email": True, "sms": True})
        assert result.status_code == 200
        assert result.json()["mode"] == "PREVIEW; NOTHING SENT"
        assert "POST" not in calls


@pytest.mark.parametrize("reason", ["opt_out", "quiet", "paid", "mismatch", "sms", "closed", "promise", "kill"])
def test_notification_safety_gates(message_case, monkeypatch, reason):
    m, ref = message_case
    calls = provider_stub(monkeypatch, state="paid" if reason == "paid" else "created",
                          recipient="other@example.com" if reason == "mismatch" else "person@example.com")
    if reason == "opt_out":
        suppression.suppress("person@example.com", NOW, path=m.suppression)
    if reason == "closed":
        D.stop(C.rebuild(ref, m.cfg, m.ledger), D.STOP_RECOVERED, NOW, path=m.ledger)
    if reason == "promise":
        promises.record(ref, NOW+timedelta(days=1), NOW, 1000000, path=m.promise)
    if reason == "kill":
        import src.policy
        monkeypatch.setattr(src.policy, "load_policy", lambda: {"bounds": {"global_kill_switch": True}})
    with pytest.raises(SendBlocked):
        send_link(ref, "sms" if reason == "sms" else "email", NOW.replace(hour=23) if reason == "quiet" else NOW, m, m.env)
    assert not any(method == "POST" for method, _ in calls)


def test_connected_webhook_is_scoped_and_signed(app, monkeypatch):
    with TestClient(app, headers=HEADERS) as c:
        r = connect(c, monkeypatch, webhook_secret="connection-signing-secret")
        a = app.state.accounts.get(c.cookies[COOKIE])
        payload = json.dumps({"event": "order.paid", "created_at": int(NOW.timestamp()), "payload": {"order": {"entity": {"id": "order_webhook", "amount": 100000}}}}).encode()
        signature = hmac.new(b"connection-signing-secret", payload, hashlib.sha256).hexdigest()
        path = r.json()["webhook_path"]
        assert c.post(path, content=payload, headers={"x-razorpay-signature": "bad"}).status_code == 400
        assert c.post(path, content=payload, headers={"x-razorpay-signature": signature}).status_code == 200
        assert a.webhooks.exists()
        assert not merchants.get().webhooks.exists()
