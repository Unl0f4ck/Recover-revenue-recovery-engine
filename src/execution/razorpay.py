"""Action execution. SPEC §11.

Every action is tagged REAL or SIMULATED in the audit record and rendered as
such in the UI (§11). No exceptions, and no action may be reported as REAL
unless an API call actually succeeded.

  REAL       Standard Payment Link creation in TEST MODE. The link is really
             created, has a real short_url, and is visible on the dashboard.
  SIMULATED  PSP/gateway rerouting, rail switching, retry effectiveness. These
             need merchant-side infrastructure we do not have.

METHOD RESTRICTION IS NOT AVAILABLE ON PAYMENT LINKS -- verified Day 4 against
https://razorpay.com/docs/api/payments/payment-links/create-standard/ . The
create API exposes no request field for limiting which methods a customer may
use; `method` appears only in the RESPONSE, reporting what was eventually used.
Passing `options.checkout.method` is accepted and silently ignored, which is
the worst kind of failure: it looks like it worked.

§11 assumed this was possible and treats it as what makes ALTERNATE_METHOD_LINK
"a genuine live demo rather than a simulated one". That premise is wrong. The
link is genuinely REAL; the RAIL RESTRICTION on it is not, and is reported as
such rather than claimed. See WORKLOG 27 Aug for the Checkout-JS route that
would make the restriction genuine.

Test mode caps Payment Links at roughly 30 per business. We create ONE or TWO
live demo links and build no reuse logic against the cap (§11).

Leakage (§1.2 extended): this module never imports the generator, the efficacy
matrix or the ledger. It is handed an action and an amount; it does not know
whether the diagnosis behind them was right.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API = "https://api.razorpay.com/v1"
REPO = Path(__file__).resolve().parents[2]

# Methods Razorpay's checkout customization understands.
ALL_METHODS = ("card", "netbanking", "upi", "wallet")

# Stamped into `notes.source` on every link this system creates, and read back
# at the ingestion seam so we never treat our own recovery link as revenue at
# risk. Defined here, beside the writer, so the two cannot drift apart.
RECOVERY_TAG = "causal-payment-recovery"


class NoCredentials(RuntimeError):
    pass


def load_env(path: Path | None = None) -> dict[str, str]:
    """Read .env without a dependency. Never logged, never echoed."""
    out = dict(os.environ)
    f = path or (REPO / ".env")
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.strip())
    return out


def _auth_header(env: dict[str, str]) -> str:
    kid, secret = env.get("RAZORPAY_KEY_ID"), env.get("RAZORPAY_KEY_SECRET")
    if not kid or not secret or "xxxx" in kid:
        raise NoCredentials("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in .env")
    if not kid.startswith("rzp_test_"):
        # Refuse to touch a live key. Nothing in this project should ever run
        # against real money.
        raise NoCredentials(f"refusing non-test key (prefix {kid[:9]!r})")
    return "Basic " + base64.b64encode(f"{kid}:{secret}".encode()).decode()


RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_RETRIES = 5


def _call(method: str, path: str, body: dict | None, env: dict[str, str],
          timeout: int = 25, retries: int = MAX_RETRIES) -> dict:
    """One HTTP call, with backoff on the failures that are worth retrying.

    Razorpay rate-limits readily and returns 429 rather than queueing. Without
    this, a single burst -- a console build reading payments, orders, links and
    invoices back to back -- fails partway through and the caller sees an
    exception where it expected a book. That is not a rare edge: it happened on
    the first build after the invoices stream was added.

    Only reads are retried. A 5xx or timeout after a write may follow a
    committed operation. Payment Link creates use a durable operation claim
    and reference lookup; the transport never replays a POST by itself.
    """
    data = json.dumps(body).encode() if body is not None else None
    # A timeout/5xx can follow a committed POST. Payment Links has no generic
    # idempotency header: durable claims and reference lookup handle recovery.
    if method.upper() not in ("GET", "HEAD"):
        retries = 0
    delay = 1.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            f"{API}{path}", data=data, method=method,
            headers={"Authorization": _auth_header(env),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode()
            # A 429 is two different things wearing one status code. "Too many
            # requests" clears on its own and backing off is exactly right.
            # "test mode limit of N reached" does not clear until links are
            # deleted, so retrying spends five sleeps -- about half a minute --
            # to arrive at the same failure, and buries the one line that says
            # what actually went wrong.
            if e.code == 429 and "limit of" in detail and "reached" in detail:
                raise RuntimeError(f"razorpay {e.code}: {detail}") from None
            if e.code in RETRY_STATUS and attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 16.0)
                continue
            raise RuntimeError(f"razorpay {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            # Read retries are safe. Writes have retries=0 and return the
            # uncertainty to the operation journal/campaign layer.
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 16.0)
                continue
            raise RuntimeError(f"razorpay transport failure: {e}") from None
    raise RuntimeError("razorpay: retries exhausted")


@dataclass(frozen=True)
class ExecutionResult:
    mode: str                       # "REAL" | "SIMULATED"
    action: str
    reference: str | None           # payment link id, when REAL
    url: str | None
    detail: str
    # Whether the failing rail was actually blocked. False on Payment Links.
    # Separate from `mode` on purpose: a REAL link whose restriction did not
    # apply must not be reportable as a rail-restricted demo.
    rail_restriction_enforced: bool = False

    def as_audit_fields(self) -> dict:
        return {"execution_mode": self.mode, "execution_reference": self.reference,
                "execution_url": self.url, "execution_detail": self.detail,
                "rail_restriction_enforced": self.rail_restriction_enforced}


def enabled_methods(exclude: list[str]) -> dict[str, str]:
    """Razorpay Checkout's method map: "1" enables, "0" disables.

    Retained for the Checkout-JS demo page, where it IS honoured. It is NOT
    sent on Payment Links, where the field does not exist.
    """
    return {m: ("0" if m in exclude else "1") for m in ALL_METHODS}


def create_recovery_link(amount_paise: int, exclude_methods: list[str],
                         description: str, env: dict[str, str] | None = None,
                         notify: dict | None = None,
                         customer: dict | None = None,
                         notes: dict | None = None) -> ExecutionResult:
    """REAL execution: a test-mode Payment Link.

    `exclude_methods` is recorded as INTENT and carried in `notes` so the audit
    trail shows what the policy wanted. It is NOT enforced -- Payment Links have
    no method-restriction field (see module docstring). The returned result says
    so explicitly, so nothing downstream can mistake the link for a
    rail-restricted one.

    DELIVERY. `notify` now reaches the caller rather than being hard-coded off.
    For most of this project nothing was ever sent to anyone: every link was
    created with `notify: {sms: false, email: false}`, which was the right
    default while there was no opt-out list and no delivery ledger. Both now
    exist, so the decision belongs to the caller -- and until a caller asks, the
    default is still silence.

    A link that is created but never delivered is a contact that did not
    happen, and the console says so. Turning this on is what makes the
    escalation ladder real rather than a plan.
    """
    env = env or load_env()
    notify = {"sms": False, "email": False, **(notify or {})}

    # NEVER NOTIFY A PLACEHOLDER. When the caller knows no customer we still
    # have to send Razorpay something -- the API wants a customer block -- so a
    # placeholder is substituted below. Asking it to DELIVER to that
    # placeholder is a different act entirely: `+919812345670` is a
    # real-format Indian mobile and it is not ours.
    #
    # `campaign.delivery_for` already refuses this case, so nothing should
    # arrive here asking for it. This is the second lock, at the boundary,
    # because it is the last code that runs before a stranger's phone rings.
    if customer is None and any(notify.values()):
        notify = {"sms": False, "email": False}
        placeholder_muted = True
    else:
        placeholder_muted = False

    # ONLY THE FIELDS WE ACTUALLY KNOW. The placeholder used to be merged
    # UNDER whatever the caller supplied, so a case where we held only an
    # e-mail still got `contact: +919812345670` written onto Razorpay's
    # customer record for that link. No message went there -- `notify.sms` is
    # false on those cases -- but storing a number that is not the customer's
    # against their payment is not something to do by accident.
    #
    # The placeholder survives for the no-contact case, where the API needs a
    # customer block and delivery has already been forced off above.
    if customer:
        who = {"name": "Recovery", **customer}
    else:
        who = {"name": "Demo Customer", "email": "demo@example.com",
               # Razorpay rejects recurring digits in a contact number, so this
               # placeholder is varied rather than 99999...
               "contact": "+919812345670"}
    body = {
        "amount": int(amount_paise),
        "currency": "INR",
        "accept_partial": False,
        "description": description[:2048],
        "customer": who,
        "notify": {"sms": bool(notify.get("sms")),
                   "email": bool(notify.get("email"))},
        "reminder_enable": False,
        "notes": {"source": RECOVERY_TAG, "mode": "test-demo",
                  "intended_rail_exclusion": ",".join(exclude_methods) or "none",
                  **(notes or {})},
    }
    key = (notes or {}).get("idempotency_key")
    if key:
        import hashlib
        from urllib.parse import urlencode
        from ..recovery.operations import Operations, PendingOperation
        body["reference_id"] = key
        journal = Operations(env.get("RECOVERY_OPERATIONS_DB") or REPO / "data/live/operations.sqlite3")
        scope = env.get("RAZORPAY_KEY_ID", "") + ":" + key
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        fresh, r = journal.claim(scope, fingerprint)
        if not fresh and r is None:
            found = _call("GET", "/payment_links?" + urlencode({"reference_id": key}), None, env)
            r = next((x for x in (found.get("payment_links") or found.get("items") or [])
                      if x.get("reference_id") == key), None)
            if r is None:
                raise PendingOperation("previous create is unresolved; reconcile or review before retrying")
        if fresh:
            r = _call("POST", "/payment_links", body, env)
        if not r.get("id") or not str(r.get("short_url", "")).startswith("https://"):
            raise PendingOperation("provider did not return a usable payment link; reconcile before retrying")
        journal.finish(scope, r)
    else:
        r = _call("POST", "/payment_links", body, env)
        if not r.get("id") or not str(r.get("short_url", "")).startswith("https://"):
            raise RuntimeError("provider did not return a usable payment link")
    sent = [k for k in ("sms", "email") if notify.get(k)]
    return ExecutionResult(
        mode="REAL", action="RECOVERY_LINK",
        reference=r.get("id"), url=r.get("short_url"),
        detail=(f"link created; intended rail exclusion "
                f"[{','.join(exclude_methods) or 'none'}] NOT enforced -- "
                f"Payment Links expose no method-restriction field; "
                f"delivery requested via {'+'.join(sent) if sent else 'nothing'}"
                + (" (delivery refused: no contact on this case, so the "
                   "placeholder customer would have been messaged)"
                   if placeholder_muted else "")),
        rail_restriction_enforced=False)


def create_alternate_method_link(amount_paise: int, exclude_methods: list[str],
                                 description: str,
                                 env: dict[str, str] | None = None
                                 ) -> ExecutionResult:
    """Backwards-compatible alias. Delivers nothing, as it always did."""
    return create_recovery_link(amount_paise, exclude_methods, description, env)


def simulate(action: str, detail: str) -> ExecutionResult:
    """SIMULATED execution. PSP rerouting and rail switching need merchant-side
    infrastructure; they are modelled, not performed.
    """
    return ExecutionResult(mode="SIMULATED", action=action, reference=None,
                           url=None, detail=detail)


def fetch_payment_link(link_id: str, env: dict[str, str] | None = None) -> dict:
    return _call("GET", f"/payment_links/{link_id}", None, env or load_env())


def credentials_available(env: dict[str, str] | None = None) -> bool:
    try:
        _auth_header(env or load_env())
        return True
    except NoCredentials:
        return False
