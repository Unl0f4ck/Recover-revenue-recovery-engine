"""LLM narration. SPEC §2: post-decision only.

*** THE LLM NEVER DECIDES WHY A PAYMENT FAILED. ***

That sentence is §16's architecture line and it is enforced here structurally,
not by prompt discipline. This module is handed a decision that has ALREADY been
made -- diagnosis, action, bounds -- and asked to phrase it. It has no path back
into the pipeline:

  - it returns a string, and nothing reads that string except the UI and the
    audit trail;
  - it is never imported by src/attribution/, src/detector/ or src/policy.py
    (tests/test_leakage.py enforces this by module name);
  - it never sees the ledger, the efficacy matrix or the generator config, so
    it cannot narrate anything the decision layer did not already conclude.

A DETERMINISTIC TEMPLATE IS THE DEFAULT. The API call is optional and additive.
If the key is missing, the network is down, or the response is malformed, the
template renders and the system is unaffected -- narration must never be able to
break or delay a recovery decision.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = "https://api.anthropic.com/v1/messages"
# Superseded by src/ai/. Kept because the narration prompt and its
# post-decision discipline are still the right shape, but the live
# path no longer calls this and the model here was a generation
# behind while nothing noticed -- because nothing was calling it.
MODEL = "claude-sonnet-5"
MAX_TOKENS = 300

ACTION_PHRASE = {
    "SAME_RAIL_RETRY": "retry on the same rail",
    "SWITCH_PSP": "route new attempts through a different gateway",
    "ALTERNATE_METHOD_LINK": "offer the customer a payment link on a working rail",
    "BACKOFF_REPRESENT": "hold and re-present shortly",
    "NO_ACTION": "take no automated action",
}

FAMILY_PHRASE = {
    "issuer_degradation": "the issuing bank is failing to authorise",
    "psp_degradation": "the payment gateway is degraded",
    "card_auth_spike": "card authentication is failing at the issuer",
    "upi_network_degradation": "the UPI network is failing to resolve",
}


@dataclass(frozen=True)
class Narration:
    text: str
    source: str            # "template" | "llm"
    model: str | None = None


def _template(rec: dict) -> str:
    """The deterministic narration. Always available, always correct."""
    cause = rec.get("cause_node") or "no single node"
    family = rec.get("mechanism_family")
    level = rec.get("level_used", "?")
    chosen = rec.get("action_chosen", "NO_ACTION")
    executed = rec.get("action_executed", chosen)
    cells = rec.get("cells") or []
    at_risk = rec.get("amount_at_risk_paise", 0) / 100.0

    if family is None or rec.get("cause_node") is None:
        body = (f"Detected a failure spike across {len(cells)} cell(s) with "
                f"dominant source `{rec.get('dominant_source')}`, but the "
                f"evidence did not separate the candidate explanations. "
                f"Escalated for a human rather than guessing.")
    else:
        body = (f"{FAMILY_PHRASE.get(family, family).capitalize()}, localised to "
                f"{cause} at rung {level}. Affected {len(cells)} cell(s), "
                f"Rs {at_risk:,.0f} at risk.")

    tail = f" Chose to {ACTION_PHRASE.get(chosen, chosen)}."
    if executed != chosen:
        fired = ", ".join(rec.get("bounds_fired") or []) or "a policy bound"
        tail += (f" That action was withheld ({fired}); "
                 f"{ACTION_PHRASE.get(executed, executed)} instead.")
    if rec.get("execution_mode") == "REAL" and not rec.get("rail_restriction_enforced",
                                                           True):
        tail += (" The payment link is real, but the rail restriction on it is "
                 "not enforced -- Payment Links expose no method-restriction "
                 "field.")
    return body + tail


def _prompt(rec: dict, template: str) -> str:
    return (
        "You are writing one short operator-facing note about a payment-recovery "
        "decision that has ALREADY been made. You are not deciding anything.\n\n"
        "Rules:\n"
        "- Do not speculate about causes beyond the diagnosis given.\n"
        "- Do not suggest a different action.\n"
        "- If the diagnosis is UNKNOWN, say so plainly; do not invent a cause.\n"
        "- Two sentences maximum, plain English, no bullet points, no preamble.\n\n"
        f"Decision record:\n{json.dumps(rec, indent=2, default=str)}\n\n"
        f"A deterministic rendering of the same facts:\n{template}\n\n"
        "Rewrite it so an on-call engineer can read it at a glance."
    )


def _env() -> dict[str, str]:
    out = dict(os.environ)
    f = REPO / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.strip())
    return out


def narrate(rec: dict, use_llm: bool = True, timeout: int = 20) -> Narration:
    """Narrate one audit record.

    Falls back to the template on ANY failure -- missing key, network error,
    malformed response. Narration is decoration; it may never be able to break
    or delay a recovery decision.
    """
    template = _template(rec)
    if not use_llm:
        return Narration(template, "template")

    key = _env().get("ANTHROPIC_API_KEY", "")
    if not key or "xxxx" in key:
        return Narration(template, "template")

    body = json.dumps({
        "model": MODEL, "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": _prompt(rec, template)}],
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in data.get("content", [])).strip()
        return Narration(text, "llm", MODEL) if text else Narration(template, "template")
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return Narration(template, "template")


def narrate_all(records: list[dict], use_llm: bool = False) -> list[dict]:
    """Attach narration to audit records for the UI. Template by default: the
    UI must render offline and identically every time.
    """
    out = []
    for rec in records:
        n = narrate(rec, use_llm=use_llm)
        out.append({**rec, "narration": n.text, "narration_source": n.source})
    return out
