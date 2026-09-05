"""One local service for the console, durable batch evidence and verified callbacks.

Operator APIs require a custom header and same-origin browser requests. Remote
operators additionally require RECOVERY_ADMIN_TOKEN. Public routes accept only
signed customer tokens or verified Razorpay webhook bodies. Submitted account
secrets are held in server memory and never returned to the UI.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Literal

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..execution import razorpay as rz
from ..recovery import campaign as C, ledger as L, merchants, channels, view, review, runlock, tokens, webhooks
from ..recovery import dunning as D
from ..ai import replies, provider
from .accounts import Accounts, COOKIE

REPO = Path(__file__).resolve().parents[2]


def configured_secret(value):
    return bool(value and "xxxx" not in value.lower())


class BatchRequest(BaseModel):
    n: int = Field(default=240, ge=20, le=500)
    days: int = Field(default=28, ge=7, le=35)
    seed: int = Field(default=20260905, ge=0, le=2**31-1)


class ReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    apply: bool = False
    proposal_id: str | None = None


class PassRequest(BaseModel):
    execute: bool = False
    max_actions: int = Field(default=5, ge=1, le=20)
    sms: bool = False
    email: bool = False
    confirm_contact: bool = False


class AccountRequest(BaseModel):
    name: str = Field(default="My test account", min_length=1, max_length=80)
    key_id: str = Field(min_length=12, max_length=90)
    key_secret: SecretStr = Field(min_length=8, max_length=256)
    webhook_secret: SecretStr = Field(default=SecretStr(""), max_length=256)


class SendRequest(BaseModel):
    channel: str = Field(pattern="^(sms|email)$")
    confirm_contact: bool = False


class CsvRequest(BaseModel):
    csv: str = Field(min_length=1, max_length=200000)
    unit: Literal["paise", "rupees"] = "paise"


class ReviewRequest(BaseModel):
    note: str = Field(min_length=3, max_length=1000)


def create_app(root: Path | None = None, env: dict | None = None):
    env = rz.load_env() if env is None else env
    root = Path(root or REPO / "data/console")
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Recover / Revenue recovery control", version="0.2.0")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=env.get("RECOVERY_HOSTS", "localhost,127.0.0.1,testserver").split(","))
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="recovery")
    guard = threading.RLock()
    jobs = {}
    proposals = {}
    accounts = Accounts(root, env)

    def workspace(request):
        sid = request.cookies.get(COOKIE)
        return accounts.get(sid) if sid else None

    def owner(request):
        a = workspace(request)
        if not a and request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "Connect your own test account first")
        return a.owner if a else "default"

    def merchant(request):
        return workspace(request) or merchants.get()

    def credentials(request):
        a = workspace(request)
        if a:
            return a.env
        if request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "Connect your own test account first")
        return env

    def owned_jobs(request):
        identity = owner(request)
        return [j for j in jobs.values() if j.get("owner", "default") == identity]

    def save(job):
        dest = root / job["id"] / "job.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(".tmp")
        temp.write_text(json.dumps(job, default=str), encoding="utf-8")
        temp.replace(dest)

    # Completed runs survive restarts. Interrupted jobs never masquerade as complete.
    for file in root.glob("*/job.json"):
        try:
            job = json.loads(file.read_text(encoding="utf-8"))
            if job["status"] in ("running", "queued"):
                job.update(status="interrupted", error="server stopped during this run; start a new batch")
            jobs[job["id"]] = job
        except (ValueError, KeyError):
            continue

    @app.middleware("http")
    async def headers(request, call_next):
        if request.url.path.startswith("/api/"):
            try:
                length = int(request.headers.get("content-length", "0") or 0)
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, status_code=400)
            if length > 250000:
                return JSONResponse({"detail": "Body too large"}, status_code=413)
        result = await call_next(request)
        result.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                               "X-Frame-Options": "DENY", "Cache-Control": "no-store"})
        return result

    @app.exception_handler(runlock.LockHeld)
    async def locked(request, exc):
        return JSONResponse({"detail": "Another writer is active; retry shortly"}, status_code=409)

    @app.exception_handler(rz.NoCredentials)
    async def no_credentials(request, exc):
        return JSONResponse({"detail": "Connect your Razorpay test account in Connections first"}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Validation responses must never echo a submitted API secret.
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]}
                                        for e in exc.errors()]}, status_code=422)

    def operator(request: Request):
        if request.headers.get("x-recovery-client") != "console":
            raise HTTPException(403, "Operator API requires X-Recovery-Client: console")
        origin = request.headers.get("origin")
        if origin and (urlparse(origin).netloc != request.headers.get("host") or urlparse(origin).scheme != request.url.scheme):
            raise HTTPException(403, "Cross-origin operator request refused")
        secret = env.get("RECOVERY_ADMIN_TOKEN", "")
        peer = request.client.host if request.client else ""
        remote = peer not in ("127.0.0.1", "::1", "testclient")
        if secret or remote:
            auth = request.headers.get("authorization", "").removeprefix("Bearer ")
            if len(secret) < 32 or not hmac.compare_digest(auth, secret):
                raise HTTPException(401, "Operator token required")

    auth = [Depends(operator)]

    def location(source, request):
        if source == "live":
            # Remote clients must not see the server operator's default ledger.
            credentials(request)
            m = merchant(request)
            return m.ledger, m.notifications, m.suppression, m.promise, m.review, datetime.now(C.IST)
        if source not in jobs or jobs[source]["status"] != "complete" or jobs[source].get("owner", "default") != owner(request):
            raise HTTPException(404, "Completed batch not found")
        base = root / source
        return (base / "ledger.jsonl", base / "notifications.jsonl", base / "suppression.jsonl",
                base / "promises.jsonl", base / "review.jsonl",
                datetime.fromisoformat(jobs[source]["ended"]))

    def snapshot(source, request):
        ledger, notifications, sup, promise, rev, now = location(source, request)
        cfg = merchants.get().cfg
        cs = view.campaigns(now, cfg, ledger)
        rows = L.read(ledger)
        mode = "RAZORPAY TEST MODE" if source == "live" else jobs[source].get("kind", "SIMULATED")
        grouped = {}
        for c in cs:
            group = grouped.setdefault(c.kind, {"kind": c.kind, "cases": 0, "exposure": 0, "recovered": 0})
            group["cases"] += 1
            group["exposure"] += c.at_risk_paise
            group["recovered"] += c.recovered_paise
        paid = [e for e in rows if e["event"] == L.ATTEMPT_SUCCEEDED]
        daily = {}
        for e in paid:
            day = e["at"][:10]
            daily[day] = daily.get(day, 0) + int(e["amount_paise"])
        compact = [{"reference": c.reference, "kind": c.kind, "state": c.state,
                    "amount": c.at_risk_paise, "recovered": c.recovered_paise,
                    "classification": c.invariants.decline_class,
                    "next": c.invariants.next_action, "next_due": c.invariants.next_due}
                   for c in cs]
        return {"source": source, "mode": mode, "at": now,
                "metrics": {"cases": len(cs), "at_risk": sum(c.at_risk_paise for c in cs),
                    "recovered": sum(int(e["amount_paise"]) for e in paid),
                    "won": len({e["reference"] for e in paid}),
                    "open": sum(not c.terminal for c in cs),
                    "contacts": L_contacts(rows, cfg)},
                "streams": list(grouped.values()), "campaigns": compact,
                "daily": sorted(daily.items()), "states": view.state_counts(cs),
                "review": [asdict(r) for r in review.queue(now, cfg, ledger, rev)],
                "evidence": jobs.get(source, {}).get("evidence", []),
                "job": jobs.get(source), "events": len(rows)}

    def run_batch(job, args):
        try:
            from ..sim.run import run
            from scripts import simulate_batch as checks
            with guard:
                job["status"] = "running"
                save(job)
            def progress(result, now):
                with guard:
                    job["progress"] = min(99, round(result.ticks / (args.days * 24 + 1) * 100))
            result = run(n=args.n, days=args.days, seed=args.seed, out_dir=root/job["id"], progress=progress)
            cfg, wf = merchants.get().cfg, channels.load_workflows()
            evidence = [("No contact after opt-out", checks.check_no_contact_after_opt_out(result)),
                        ("Quiet hours respected", checks.check_quiet_hours(result, wf)),
                        ("Contact ceilings respected", checks.check_contact_ceiling(result, cfg)),
                        ("Every attempt has an outcome", checks.check_write_ahead(result)),
                        ("One recovery per reference", checks.check_one_success(result))]
            with guard:
                job.update(status="complete", progress=100, ended=result.ended.isoformat(),
                           total_cases=len(result.items), rejected=len(result.declined_to_open),
                           evidence=[{"name": n, "passed": p, "detail": d} for n, (p, d) in evidence])
                save(job)
        except Exception as e:
            with guard:
                job.update(status="failed", error=f"{type(e).__name__}: batch failed; see server log")
                save(job)
            import logging
            logging.getLogger(__name__).exception("Batch failed")

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.2.0"}

    @app.get("/api/status", dependencies=auth)
    def status(request: Request):
        a = workspace(request)
        active_env = a.env if a else env
        remote_unconnected = not a and request.client and request.client.host not in ("127.0.0.1", "::1", "testclient")
        with guard:
            return {"jobs": [] if remote_unconnected else sorted(owned_jobs(request), key=lambda j: j.get("created", ""), reverse=True),
                    "razorpay": False if remote_unconnected else rz.credentials_available(active_env), "ai": provider.available(active_env),
                    "webhook": configured_secret(active_env.get("RAZORPAY_WEBHOOK_SECRET")),
                    "customer_links": configured_secret(active_env.get("RECOVERY_TOKEN_SECRET")),
                    "account": a.public() if a else {"connected": False, "name": "Server workspace"}}

    @app.post("/api/account/connect", dependencies=auth)
    def connect_account(body: AccountRequest, request: Request, response: Response):
        if request.url.scheme != "https" and request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(400, "HTTPS is required to connect an account remotely")
        sid, a = accounts.connect(body.key_id.strip(), body.key_secret.get_secret_value().strip(),
                                  body.name.strip(), body.webhook_secret.get_secret_value())
        accounts.disconnect(request.cookies.get(COOKIE))
        response.set_cookie(COOKIE, sid, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return a.public()

    @app.delete("/api/account", dependencies=auth)
    def disconnect_account(request: Request, response: Response):
        accounts.disconnect(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)
        return {"status": "disconnected", "detail": "Session credentials removed; audit records retained"}

    @app.post("/api/batches", dependencies=auth, status_code=202)
    def batches(args: BatchRequest, request: Request):
        with guard:
            if any(j["status"] in ("queued", "running") for j in jobs.values()):
                raise HTTPException(409, "A batch is already running")
            job = {"id": uuid.uuid4().hex, "status": "queued", "progress": 0,
                   "created": datetime.now(C.IST).isoformat(), "config": args.model_dump(), "owner": owner(request)}
            jobs[job["id"]] = job
            save(job)
            pool.submit(run_batch, job, args)
            return dict(job)

    @app.get("/api/snapshot", dependencies=auth)
    def get_snapshot(request: Request, source: str = "live"):
        return snapshot(source, request)

    @app.get("/api/cases/{reference}", dependencies=auth)
    def case(reference: str, request: Request, source: str = "live"):
        ledger, notifications, _, _, _, now = location(source, request)
        cfg = merchants.get().cfg
        c = next((c for c in view.campaigns(now, cfg, ledger) if c.reference == reference), None)
        if c is None:
            raise HTTPException(404, "Case not found")
        from ..recovery import notify
        return {"campaign": asdict(c), "events": L.events_for(reference, ledger),
                "terminal": c.terminal, "notifications": notify.for_reference(reference, notifications)}

    @app.get("/api/evidence", dependencies=auth)
    def evidence(request: Request, source: str = "live"):
        ledger, *_ = location(source, request)
        body = "\n".join(json.dumps(e) for e in L.read(ledger)) + "\n"
        return Response(body, media_type="application/x-ndjson", headers={
            "Content-Disposition": f'attachment; filename="recovery-{source}.jsonl"',
            "X-Content-SHA256": hashlib.sha256(body.encode()).hexdigest()})

    @app.post("/api/live/run", dependencies=auth)
    def live_run(args: PassRequest, request: Request):
        from ..recovery.runner import collect
        from ..recovery.service import live_pass, diagnose_items
        from ..ingest.razorpay_source import fetch_payments
        active_env = credentials(request)
        m = merchant(request)
        if args.execute and (args.sms or args.email) and not args.confirm_contact:
            raise HTTPException(422, "Confirm permission to contact the account's customers before sending")
        import copy
        wf = copy.deepcopy(channels.load_workflows())
        # A browser-connected account must never inherit local demo recipients.
        wf.setdefault("delivery", {})["redirect"] = {"enabled": False}
        deliver = {"sms": args.sms, "email": args.email}
        now = datetime.now(C.IST)
        with runlock.exclusive(label="web live pass"):
            items = collect(env=active_env)
            items, diagnosis = diagnose_items(items, fetch_payments(env=active_env))
            if args.execute:
                result = live_pass(items, now, m.cfg, env=active_env, **m.paths(),
                                   promise_path=m.promise, limit=args.max_actions, deliver=deliver, wf_cfg=wf)
            else:
                preview = root / "previews" / owner(request)
                result = C.run_pass(items, now, m.cfg, dry_run=True, limit=args.max_actions,
                    path=preview/"ledger.jsonl", notif_path=preview/"notifications.jsonl",
                    sup_path=m.suppression, promise_path=m.promise, deliver=deliver, wf_cfg=wf)
            return {"result": asdict(result), "diagnosis": diagnosis.conclusion,
                    "mode": ("TEST EXECUTION; NOTIFICATIONS ENABLED" if any(deliver.values()) else "TEST LINKS CREATED; NO MESSAGES") if args.execute else "PREVIEW; NOTHING SENT",
                    "delivery": deliver, "delivery_note": "Only known contact details are used. Requested is not confirmed delivery; Razorpay may restrict sandbox notifications."}

    @app.post("/api/import", dependencies=auth)
    def csv_import(body: CsvRequest, request: Request):
        from ..ingest.csv_source import read_csv
        identity = owner(request)
        identifier = uuid.uuid4().hex
        base = root / identifier
        base.mkdir()
        file = base / "input.csv"
        file.write_text(body.csv, encoding="utf-8")
        now = datetime.now(C.IST)
        result = read_csv(file, body.unit, now)
        C.run_pass(result.items, now, path=base/"ledger.jsonl", dry_run=True,
                   notif_path=base/"notifications.jsonl", sup_path=base/"suppression.jsonl",
                   promise_path=base/"promises.jsonl")
        job = {"id": identifier, "status": "complete", "progress": 100,
               "created": now.isoformat(), "ended": now.isoformat(), "kind": "CSV PREVIEW",
               "config": {}, "rejected": result.rejected, "total_cases": len(result.items), "owner": identity}
        with guard:
            jobs[identifier] = job
            save(job)
        return job

    @app.get("/api/notifications", dependencies=auth)
    def notification_log(request: Request, source: str = "live"):
        from ..recovery import notify
        _, path, *_ = location(source, request)
        latest = {}
        for row in notify.read(path):
            if row.get("channel") in ("sms", "email"):
                latest[(row["reference"], row["attempt_no"], row["channel"])] = row
        return {"messages": list(reversed(list(latest.values()))),
                "note": "Requested is not confirmed delivery. Unknown requests are held, never automatically resent."}

    @app.post("/api/cases/{reference}/notify", dependencies=auth)
    def send_notification(reference: str, body: SendRequest, request: Request):
        from ..recovery.messaging import send_link, SendBlocked
        active_env = credentials(request)
        if not body.confirm_contact:
            raise HTTPException(422, "Confirm permission to contact this customer")
        with runlock.exclusive(label="operator notification"):
            try:
                return send_link(reference, body.channel, datetime.now(C.IST), merchant(request), active_env)
            except SendBlocked as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/cases/{reference}/reply", dependencies=auth)
    def reply(reference: str, body: ReplyRequest, request: Request, source: str = "live"):
        ledger, _, sup, promise, rev, now = location(source, request)
        seq = C.rebuild(reference, merchants.get().cfg, ledger)
        if seq is None:
            raise HTTPException(404, "Case not found")
        proposal_id = body.proposal_id
        if body.apply:
            with guard:
                proposal = proposals.get(proposal_id)
                if not proposal or proposal["reference"] != reference or proposal["source"] != source or proposal["text"] != body.text or proposal.get("owner") != owner(request):
                    raise HTTPException(409, "Read this reply before applying its proposal")
                if (datetime.now(C.IST) - proposal["created"]).total_seconds() > 900:
                    raise HTTPException(409, "Proposal expired; read the reply again")
                if proposal.get("result"):
                    return proposal["result"]
                reading = proposal["reading"]
        # Explicit STOP is honored even if the model is unavailable.
        elif body.text.strip().lower() in ("stop", "unsubscribe", "band karo", "do not contact me"):
            reading = replies.Reading(replies.OPT_OUT, 1.0, quote=body.text,
                                      provider="rules", model="explicit-opt-out")
        else:
            try:
                reading = replies.read_reply(body.text, now, reference=reference,
                    amount_paise=seq.at_risk_paise, provider=provider.get_provider(env))
            except provider.LLMError:
                reading = replies.unreadable(body.text, "AI unavailable; an operator must review this reply")
        if not body.apply:
            proposal_id = uuid.uuid4().hex
            with guard:
                if len(proposals) > 1000:
                    proposals.clear()
                proposals[proposal_id] = {"reference": reference, "source": source,
                    "text": body.text, "reading": reading, "created": datetime.now(C.IST), "owner": owner(request)}
        action = None
        if body.apply:
            lock = runlock.LOCK if source == "live" else root/source/"operator.lock"
            with runlock.exclusive(path=lock, label="apply customer reply"):
                # Recheck inside the same mutation lock, and cache the result
                # before releasing it. Overlapping confirmations apply once.
                with guard:
                    if proposal.get("result"):
                        return proposal["result"]
                action = replies.apply_reading(reading, reference, now, channels.load_workflows(),
                    promise_path=promise, sup_path=sup, review_path=rev,
                    customer_ref=seq.customer_ref, amount_paise=seq.at_risk_paise)
                if action.action in ("escalated", "refused") and not L.is_stopped(reference, ledger):
                    D.stop(seq, "human_review_required", now, reading.why(), ledger)
                if action.action == "suppressed":
                    from ..recovery.suppression import normalise
                    for ref in L.open_sequences(ledger):
                        other = C.rebuild(ref, merchants.get().cfg, ledger)
                        if other and normalise(other.customer_ref) == normalise(seq.customer_ref):
                            D.stop(other, D.STOP_OPTED_OUT, now, "customer STOP reply", ledger)
                result = {"reading": asdict(reading), "action": asdict(action), "proposal_id": proposal_id}
                with guard:
                    proposal["result"] = result
                return result
        result = {"reading": asdict(reading), "action": asdict(action) if action else None,
                  "proposal_id": proposal_id}
        return result

    @app.post("/api/cases/{reference}/resolve", dependencies=auth)
    def resolve(reference: str, body: ReviewRequest, request: Request, source: str = "live"):
        ledger, _, _, _, rev, now = location(source, request)
        if not L.events_for(reference, ledger):
            raise HTTPException(404, "Case not found")
        lock = runlock.LOCK if source == "live" else root/source/"operator.lock"
        with runlock.exclusive(path=lock, label="resolve review"):
            review.resolve(reference, "console-operator", now, body.note, rev)
        return {"status": "resolved", "detail": "Review closed; campaign remains stopped"}

    @app.post("/webhooks/razorpay")
    @app.post("/webhooks/razorpay/{account_id}")
    async def webhook(request: Request, account_id: str | None = None):
        a = accounts.webhook_account(account_id) if account_id else None
        active_env = a.env if a else env
        secret = active_env.get("RAZORPAY_WEBHOOK_SECRET")
        if not configured_secret(secret):
            raise HTTPException(503, "Webhook secret not configured")
        raw = await limited_body(request, 1000000)
        m = a or merchants.get()
        try:
            event = webhooks.verify(raw, dict(request.headers), secret,
                [active_env["RAZORPAY_WEBHOOK_PREVIOUS_SECRET"]] if active_env.get("RAZORPAY_WEBHOOK_PREVIOUS_SECRET") else None)
            if not event.event_id:
                from dataclasses import replace
                event = replace(event, event_id=hashlib.sha256(raw).hexdigest())
            with runlock.exclusive(label="verified webhook"):
                return asdict(webhooks.apply(event, datetime.now(C.IST), m.cfg, m.ledger, m.webhooks))
        except runlock.LockHeld:
            raise HTTPException(503, "Writer busy; retry webhook")
        except (ValueError, RuntimeError):
            raise HTTPException(400, "Invalid webhook signature or payload")

    @app.get("/opt-out", response_class=HTMLResponse)
    def opt_out_form(t: str):
        validate_token(t, tokens.OPT_OUT)
        return public_page("Stop recovery messages", '<p>You can stop messages about your payments. This does not change an outstanding balance.</p>'
            '<form method="post"><input type="hidden" name="t" value="'+html.escape(t, quote=True)+'"><button>Stop messages</button></form>')

    def validate_token(t, purpose):
        secret = env.get("RECOVERY_TOKEN_SECRET")
        if not configured_secret(secret):
            raise HTTPException(503, "Customer links not configured")
        try:
            token = tokens.verify(t, secret, datetime.now(C.IST))
            if token.purpose != purpose:
                raise ValueError()
            return token
        except (ValueError, KeyError, tokens.BadToken):
            raise HTTPException(400, "Link invalid or expired")

    @app.post("/opt-out", response_class=HTMLResponse)
    async def opt_out(request: Request):
        form = parse_qs((await limited_body(request, 8192)).decode())
        t = (form.get("t") or [""])[0]
        validate_token(t, tokens.OPT_OUT)
        with runlock.exclusive(label="customer opt-out"):
            tokens.redeem_opt_out(t, env["RECOVERY_TOKEN_SECRET"], datetime.now(C.IST), merchants.get().suppression)
        return public_page("Messages stopped", "<p>Your preference has been recorded. You will receive no further recovery messages.</p>")

    @app.get("/update-instrument", response_class=HTMLResponse)
    def update_instrument(t: str):
        token = validate_token(t, tokens.UPDATE_INSTRUMENT)
        m = merchants.get()
        seq = C.rebuild(token.reference or "", m.cfg, m.ledger)
        from ..recovery.suppression import normalise
        if seq is None or normalise(seq.customer_ref) != normalise(token.customer_ref):
            raise HTTPException(404, "No payment case belongs to this customer link")
        if L.is_stopped(seq.reference, m.ledger):
            return public_page("Campaign closed", "<p>This recovery campaign is already closed. Contact your merchant if you need help with the payment.</p>")
        events = L.events_for(seq.reference, m.ledger)
        link = next((e.get("execution_url") for e in reversed(events) if e.get("execution_url")), None)
        if not link or urlparse(link).scheme != "https":
            return public_page("Contact your merchant", "<p>No active payment link is available. Ask your merchant for a new secure payment link.</p>")
        return public_page("Choose a payment method", '<p>Continue to the payment provider to choose an instrument. We never collect card details.</p><a href="'+html.escape(link, quote=True)+'">Continue to secure payment</a>')

    @app.get("/", response_class=FileResponse)
    def index():
        return REPO / "ui/control/index.html"

    app.mount("/assets", StaticFiles(directory=REPO/"ui/control"), name="assets")
    app.state.jobs = jobs
    app.state.pool = pool
    app.state.accounts = accounts
    return app


def L_contacts(rows, cfg):
    return sum(e["event"] == L.ATTEMPT_INFLIGHT and e.get("channel") in cfg["compliance"]["contacting_channels"] for e in rows)


async def limited_body(request, size):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > size:
            raise HTTPException(413, "Body too large")
    return bytes(data)


def public_page(title, content):
    return '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width"><title>'+title+'</title><link rel="stylesheet" href="/assets/styles.css"><main class="public"><span class="eyebrow">RECOVER / CUSTOMER CONTROL</span><h1>'+title+'</h1>'+content+'</main></html>'


app = create_app()
