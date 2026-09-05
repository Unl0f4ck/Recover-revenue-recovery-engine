"""Verified, browser-scoped test connections. Secrets live in server memory only.

This is a single-process operator console, not a hosted identity service. Account
data is keyed by a hash of the verified API key ID; possession of working keys
is required to reopen that workspace. API secrets are never written to disk.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import re
import secrets
import threading

from fastapi import HTTPException
from ..execution import razorpay as rz
from ..recovery import merchants

COOKIE = "recovery-account"
TTL = timedelta(hours=8)


@dataclass
class Connection:
    owner: str
    name: str
    env: dict = field(repr=False)
    base: Path
    expires: datetime

    @property
    def cfg(self):
        return merchants.get().cfg

    def __getattr__(self, name):
        files = {"ledger": "ledger.jsonl", "notifications": "notifications.jsonl",
                 "suppression": "suppression.jsonl", "promise": "promises.jsonl",
                 "review": "review.jsonl", "webhooks": "webhooks.jsonl"}
        if name in files:
            return self.base / files[name]
        raise AttributeError(name)

    def paths(self):
        return dict(path=self.ledger, notif_path=self.notifications, sup_path=self.suppression)

    def public(self):
        return {"connected": True, "name": self.name, "mode": "test",
                "key_hint": "rzp_test_…" + self.env["RAZORPAY_KEY_ID"][-4:],
                "expires": self.expires.isoformat(), "webhook_path": f"/webhooks/razorpay/{self.owner}",
                "webhook_configured": bool(self.env.get("RAZORPAY_WEBHOOK_SECRET"))}


class Accounts:
    def __init__(self, root, server_env):
        self.root, self.server_env = Path(root), server_env
        self.sessions = {}
        self.guard = threading.RLock()

    def prune(self):
        now = datetime.now(timezone.utc)
        for key in list(self.sessions):
            if self.sessions[key].expires <= now:
                del self.sessions[key]

    def connect(self, key_id, key_secret, name, webhook_secret=""):
        if not re.fullmatch(r"rzp_test_[A-Za-z0-9]{4,80}", key_id):
            raise HTTPException(422, "Only Razorpay test-mode API keys (rzp_test_) are accepted")
        if webhook_secret and "xxxx" in webhook_secret.lower():
            raise HTTPException(422, "Replace the placeholder webhook secret before connecting")
        credentials = {"RAZORPAY_KEY_ID": key_id, "RAZORPAY_KEY_SECRET": key_secret}
        try:
            # A bounded GET authenticates the pair without creating or sending anything.
            rz._call("GET", "/payments?count=1", None, credentials, timeout=10, retries=0)
        except Exception:
            # Provider errors may echo request values; do not return or log them.
            raise HTTPException(400, "Could not verify test credentials. Check both keys and Razorpay availability.") from None
        owner = hashlib.sha256(key_id.encode()).hexdigest()
        base = self.root / "accounts" / owner
        account_env = {k: v for k, v in self.server_env.items()
                       if not k.startswith(("RAZORPAY_", "RECOVERY_"))}
        account_env.update(credentials, RECOVERY_OPERATIONS_DB=str(base / "operations.db"))
        if webhook_secret:
            account_env["RAZORPAY_WEBHOOK_SECRET"] = webhook_secret
        connection = Connection(owner, name, account_env, base, datetime.now(timezone.utc) + TTL)
        with self.guard:
            self.prune()
            if len(self.sessions) >= 100:
                raise HTTPException(429, "Connection capacity reached; disconnect an unused browser")
            sid = secrets.token_urlsafe(32)
            self.sessions[sid] = connection
        return sid, connection

    def get(self, sid):
        with self.guard:
            self.prune()
            account = self.sessions.get(sid)
            if not account:
                raise HTTPException(401, "Account connection expired; reconnect your test account")
            return account

    def disconnect(self, sid):
        with self.guard:
            self.sessions.pop(sid, None)

    def webhook_account(self, owner):
        with self.guard:
            self.prune()
            candidates = [a for a in self.sessions.values() if a.owner == owner]
            if not candidates:
                raise HTTPException(503, "Reconnect this account before retrying the webhook")
            return candidates[-1]
