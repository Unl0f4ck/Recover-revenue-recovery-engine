"""Model providers, behind one small interface.

Gemini first because its free tier is what this project has, Anthropic kept
because the narration path already spoke it, and a deterministic stub so every
test runs with no key and no network.

STRUCTURED OUTPUT, NOT FREE TEXT. Every call here asks for JSON against a
schema. Parsing a model's prose for a date is the classic way this goes wrong:
it works in the demo and fails on the reply that says "next Friday, or the
Monday after if the invoice is wrong". A schema turns a language problem into a
parsing problem the provider handles, and anything that comes back malformed is
an error rather than a guess.

NO SDK, ON PURPOSE. One `urllib` POST per provider, matching how the Razorpay
client already works. It keeps the dependency list at numpy/scipy/yaml and
keeps the request shape visible where a reader can check it against the docs.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

REPO = Path(__file__).resolve().parents[2]

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# VERIFIED AGAINST THE LIVE API, not against the docs page.
#
# The first version of this file was built from a documentation summary and was
# wrong twice over: it posted to `/v1beta/interactions`, which this key cannot
# reach at all (the call simply hung), and it did so with a request body that
# endpoint would not have understood either. Listing `/v1beta/models` with the
# real key settled it in one call -- every usable model advertises
# `generateContent`, and that is the endpoint.
#
# Model choice is also empirical. On this key:
#   gemini-2.5-flash    404, "no longer available to new users"
#   gemini-3.7-flash    503, "currently experiencing high demand"
#   gemini-flash-latest 429 after 20 calls -- see below
#   gemini-3.5-flash    works, ~2s, and kept working after the alias died
#
# `gemini-flash-latest` was the default until it started returning 429 in 0.3s.
# The quota detail named the reason: GenerateRequestsPerDayPerProjectPerModel
# -FreeTier, value 20. The alias points at the newest model, which is exactly
# the one with the smallest free allowance -- twenty calls a day is a demo, not
# a service. `gemini-3.5-flash` is a generation back and has real headroom, so
# it leads and the alias sits behind it for the day the quota resets.
#
# A free tier hands you a 503 or a 429 sooner or later, so one model is not a
# plan; the chain below is the plan.
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_FALLBACKS = ("gemini-flash-latest", "gemini-3.7-flash")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-5"

RETRY_STATUS = (429, 500, 502, 503, 504)


class LLMError(RuntimeError):
    """The call failed or came back unusable."""


class LLMUnavailable(LLMError):
    """No provider is configured. Not an error -- a state to handle."""


@dataclass
class Result:
    data: dict
    provider: str
    model: str
    raw: str = ""
    extra: dict = field(default_factory=dict)


def load_env(path: Path | None = None) -> dict[str, str]:
    out = dict(os.environ)
    f = path or (REPO / ".env")
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.strip())
    return out


def _usable(key: str | None) -> bool:
    """A placeholder is not a key.

    `.env.example` ships `sk-ant-xxxxx`, and for a while that placeholder sat in
    the real `.env` making the narration path look configured while every call
    silently fell back to a template. A key with x's in it is not set.
    """
    if not key or len(key) < 20:
        return False
    return "xxxx" not in key.lower()


class Provider(Protocol):
    name: str
    model: str

    def ready(self) -> bool: ...

    def json_call(self, prompt: str, schema: dict,
                  system: str = "", timeout: int = 20) -> Result: ...


def _post(url: str, body: dict, headers: dict, timeout: int,
          retries: int = 3) -> dict:
    data = json.dumps(body).encode()
    delay = 1.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode(errors="replace")
            if e.code in RETRY_STATUS and attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            raise LLMError(f"{e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            raise LLMError(f"transport failure: {e}") from None
    raise LLMError("retries exhausted")


class Gemini:
    name = "gemini"

    def __init__(self, env: dict | None = None, model: str = GEMINI_MODEL,
                 fallbacks: tuple[str, ...] = GEMINI_FALLBACKS):
        self.env = load_env() if env is None else env
        self.model = model
        self.fallbacks = fallbacks

    @property
    def key(self) -> str:
        return self.env.get("GEMINI_API_KEY") or self.env.get("GOOGLE_API_KEY") or ""

    def ready(self) -> bool:
        return _usable(self.key)

    def json_call(self, prompt: str, schema: dict, system: str = "",
                  timeout: int = 30) -> Result:
        if not self.ready():
            raise LLMUnavailable("GEMINI_API_KEY is not set in .env")

        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _gemini_schema(schema),
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        last = None
        for model in (self.model, *self.fallbacks):
            url = f"{GEMINI_BASE}/{model}:generateContent"
            try:
                # One attempt per model, not three. The retry loop inside _post
                # and the fallback loop out here are both retries, and nesting
                # them multiplies: three attempts at a 30s timeout before the
                # second model is even tried is a two-minute wait for what is
                # meant to be a two-second call. Trying a DIFFERENT model is the
                # better retry anyway, because the usual reason for failure is
                # that this particular model is out of quota or at capacity.
                raw = _post(url, body, {"x-goog-api-key": self.key}, timeout,
                            retries=0)
            except LLMError as e:
                # Out of quota, at capacity, closed to new keys, or simply not
                # answering -- all reasons to try the next model rather than to
                # give up on the request. A read timeout belongs in this list:
                # two of the first four live calls timed out on a model that
                # turned out to be rate limited, and raising there wasted a
                # working fallback.
                if any(m in str(e) for m in ("503", "404", "429",
                                             "transport failure")):
                    last = e
                    continue
                raise
            return Result(data=_parse(_gemini_text(raw)), provider=self.name,
                          model=model, raw=json.dumps(raw)[:2000])
        raise LLMError(f"every Gemini model failed; last was {last}")


def _gemini_schema(schema: dict) -> dict:
    """JSON Schema -> the OpenAPI subset Gemini's responseSchema accepts.

    The difference that bites: Gemini has no union types, so the
    `{"type": ["string", "null"]}` this codebase writes for an optional date is
    rejected. It wants `{"type": "string", "nullable": true}`. Translated here
    rather than in every caller, so the schemas stay ordinary JSON Schema and
    the Anthropic path keeps working unchanged.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, list):
            real = [t for t in v if t != "null"]
            out["type"] = real[0] if real else "string"
            if "null" in v:
                out["nullable"] = True
        elif k == "properties" and isinstance(v, dict):
            out["properties"] = {pk: _gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out["items"] = _gemini_schema(v)
        else:
            out[k] = v
    return out


class Anthropic:
    name = "anthropic"

    def __init__(self, env: dict | None = None, model: str = ANTHROPIC_MODEL):
        self.env = load_env() if env is None else env
        self.model = model

    @property
    def key(self) -> str:
        return self.env.get("ANTHROPIC_API_KEY", "")

    def ready(self) -> bool:
        return _usable(self.key)

    def json_call(self, prompt: str, schema: dict, system: str = "",
                  timeout: int = 20) -> Result:
        if not self.ready():
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set in .env")
        body = {
            "model": self.model, "max_tokens": 512,
            "system": (system + "\n\nReply with JSON matching this schema and "
                       "nothing else:\n" + json.dumps(schema)),
            "messages": [{"role": "user", "content": prompt}],
        }
        raw = _post(ANTHROPIC_URL, body,
                    {"x-api-key": self.key,
                     "anthropic-version": "2023-06-01"}, timeout)
        text = "".join(b.get("text", "") for b in raw.get("content", []))
        return Result(data=_parse(text), provider=self.name, model=self.model,
                      raw=text[:2000])


def _gemini_text(raw: dict) -> str:
    """Pull the text out of whichever response shape came back.

    The API has moved between `candidates[].content.parts[].text` and the newer
    interactions shape, and a client that knows only one of them breaks on an
    upgrade nobody told it about. Try both, then fail loudly.
    """
    # The shape this key actually returns, checked with curl.
    for cand in raw.get("candidates") or []:
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p["text"] for p in parts if isinstance(p.get("text"), str))
        if text:
            return text
        # A candidate with no text usually means the model stopped for a
        # reason worth surfacing -- a safety block or a token ceiling -- and
        # reporting "no text" would hide it.
        if cand.get("finishReason") not in (None, "STOP"):
            raise LLMError(f"model stopped: {cand['finishReason']}")
    if isinstance(raw.get("output_text"), str):
        return raw["output_text"]
    if raw.get("promptFeedback", {}).get("blockReason"):
        raise LLMError(f"prompt blocked: "
                       f"{raw['promptFeedback']['blockReason']}")
    raise LLMError(f"no text in response: {json.dumps(raw)[:300]}")


def _parse(text: str) -> dict:
    """JSON or nothing.

    Models sometimes wrap JSON in a fenced block even when asked not to, so that
    one wrapper is stripped. Nothing else is repaired: a response we have to
    guess at is a response we should not act on.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        t = t.rsplit("```", 1)[0]
    try:
        got = json.loads(t)
    except json.JSONDecodeError as e:
        raise LLMError(f"response was not JSON: {t[:200]}") from None
    if not isinstance(got, dict):
        raise LLMError(f"expected a JSON object, got {type(got).__name__}")
    return got


class Stub:
    """Deterministic stand-in. Every test runs on this: no key, no network.

    It is not a mock of a model -- it returns whatever it was handed. Tests that
    need a specific answer supply it, which keeps them testing OUR handling of a
    response rather than a provider's behaviour.
    """
    name = "stub"
    model = "stub"

    def __init__(self, response: dict | None = None, fail: bool = False):
        self.response = response or {}
        self.fail = fail
        self.calls: list[dict] = []

    def ready(self) -> bool:
        return not self.fail

    def json_call(self, prompt: str, schema: dict, system: str = "",
                  timeout: int = 20) -> Result:
        self.calls.append({"prompt": prompt, "schema": schema, "system": system})
        if self.fail:
            raise LLMUnavailable("stub configured to be unavailable")
        return Result(data=dict(self.response), provider=self.name,
                      model=self.model)


def available(env: dict | None = None) -> list[str]:
    env = load_env() if env is None else env
    return [p.name for p in (Gemini(env), Anthropic(env)) if p.ready()]


def get_provider(env: dict | None = None, prefer: str = "gemini") -> Provider:
    """The first configured provider, or raise.

    Raises rather than returning a silent no-op, because a system that quietly
    stops understanding replies looks identical to one where no customer ever
    replied -- and the caller can decide to degrade gracefully with far better
    information than this function has.
    """
    env = load_env() if env is None else env
    order = [Gemini(env), Anthropic(env)]
    if prefer == "anthropic":
        order.reverse()
    for p in order:
        if p.ready():
            return p
    raise LLMUnavailable(
        "no model provider configured. Add GEMINI_API_KEY to .env "
        "(free key: https://aistudio.google.com/apikey) or ANTHROPIC_API_KEY.")
