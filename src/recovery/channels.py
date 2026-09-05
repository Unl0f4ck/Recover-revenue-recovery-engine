"""Channels: how we reach someone, what it costs, and what binds it.

Pulled out of the schedule because a channel is not just a label on a rung. It
carries a cost, a legal window and an availability, and those differ enough
between an SMS and a phone call that one global rule cannot serve both.

VOICE IS THE REASON THIS MODULE EXISTS. In India, TRAI restricts commercial
calls to 09:00-21:00 and honours the DND registry. That window is NARROWER than
our own messaging window (08:00-22:00) at both ends. A single quiet-hours rule
would therefore either over-restrict SMS -- costing recovery for no reason -- or
under-restrict calls, which is a regulatory breach rather than a discourtesy.
So the window is per channel, and the stricter one wins where they overlap.

Voice is also an order of magnitude dearer than SMS and far more intrusive, so
it is gated three ways beyond the clock: a minimum exposure, a requirement that
softer channels were tried first, and an explicit availability flag. On this
account that flag is FALSE -- no telephony provider is connected -- and the
ladder records the call as a step it would take rather than one it placed.
Reporting a call we cannot make would be the worst kind of demo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from ..yamlcache import load_yaml

CONFIG = Path(__file__).resolve().parents[2] / "config"
IST = timezone(timedelta(hours=5, minutes=30))


def load_workflows() -> dict:
    return load_yaml(CONFIG / "workflows.yaml")


@dataclass(frozen=True)
class Channel:
    key: str
    plain: str
    contacting: bool = False
    cost_paise: int = 0
    hours_ist: tuple[int, int] | None = None
    respects_dnd: bool = False
    min_at_risk_paise: int = 0
    requires_prior_contact: bool = False
    language: str = ""
    executable: bool = True
    terminal: bool = False
    unavailable_reason: str = ""


def get(key: str, cfg: dict | None = None) -> Channel:
    cfg = cfg or load_workflows()
    spec = (cfg.get("channels") or {}).get(key, {})
    hours = spec.get("hours_ist")
    return Channel(
        key=key,
        plain=spec.get("plain", key.replace("_", " ")),
        contacting=bool(spec.get("contacting", False)),
        cost_paise=int(spec.get("cost_paise", 0)),
        hours_ist=tuple(hours) if hours else None,
        respects_dnd=bool(spec.get("respects_dnd", False)),
        min_at_risk_paise=int(spec.get("min_at_risk_paise", 0)),
        requires_prior_contact=bool(spec.get("requires_prior_contact", False)),
        language=spec.get("language", ""),
        executable=bool(spec.get("executable", True)),
        terminal=bool(spec.get("terminal", False)),
        unavailable_reason=" ".join(
            str(spec.get("unavailable_reason", "")).split()),
    )


def all_channels(cfg: dict | None = None) -> list[Channel]:
    cfg = cfg or load_workflows()
    return [get(k, cfg) for k in (cfg.get("channels") or {})]


@dataclass(frozen=True)
class ChannelDecision:
    allowed: bool
    reason: str = ""
    defer_until: datetime | None = None
    executable: bool = True


def permitted_now(ch: Channel, now: datetime) -> bool:
    if not ch.hours_ist:
        return True
    start, end = ch.hours_ist
    h = now.astimezone(IST).hour
    return start <= h < end


def next_window(ch: Channel, now: datetime) -> datetime:
    """First moment this channel may be used again."""
    if not ch.hours_ist or permitted_now(ch, now):
        return now
    start, _ = ch.hours_ist
    local = now.astimezone(IST)
    target = local.replace(hour=start, minute=0, second=0, microsecond=0)
    if target <= local:
        target = target + timedelta(days=1)
    return target.astimezone(now.tzinfo or timezone.utc)


def authorize(ch: Channel, now: datetime, at_risk_paise: int,
              prior_contacts: int) -> ChannelDecision:
    """Channel-specific gate, applied on top of the shared compliance rules.

    Order matters. Availability is checked LAST, so a call that could not be
    placed anyway is still tested against its legal window and its exposure
    floor -- otherwise an unavailable channel would silently skip the checks
    that make it safe, and turning it on later would enable an untested path.
    """
    if ch.min_at_risk_paise and at_risk_paise < ch.min_at_risk_paise:
        return ChannelDecision(
            False, f"{ch.plain} is reserved for exposure over "
                   f"Rs {ch.min_at_risk_paise/100:,.0f}; this is "
                   f"Rs {at_risk_paise/100:,.0f}")
    if ch.requires_prior_contact and prior_contacts == 0:
        return ChannelDecision(
            False, f"{ch.plain} comes after a softer approach, not instead "
                   f"of one")
    if not permitted_now(ch, now):
        nxt = next_window(ch, now)
        start, end = ch.hours_ist
        return ChannelDecision(
            False,
            f"outside the {start:02d}:00-{end:02d}:00 window for "
            f"{ch.plain}" + (" (TRAI)" if ch.respects_dnd else ""),
            defer_until=nxt)
    if not ch.executable:
        return ChannelDecision(
            True, ch.unavailable_reason or f"{ch.plain} is not connected",
            executable=False)
    return ChannelDecision(True, f"{ch.plain} permitted")


def _redirect_contacts() -> dict:
    """Read the redirect contacts from the gitignored local file.

    Kept out of `workflows.yaml` because they are real personal addresses and a
    real phone number, and this repository is meant to be read by other people.
    A missing file is not an error -- it means the redirect is off, which is the
    right default for anyone who clones this.
    """
    f = CONFIG / "redirect.local.yaml"
    if not f.exists():
        return {}
    try:
        return load_yaml(f) or {}
    except Exception:                                    # noqa: BLE001
        return {}


def delivery(cfg: dict | None = None) -> dict:
    """How the provider should be asked to deliver a link, if at all."""
    cfg = cfg or load_workflows()
    d = dict(cfg.get("delivery") or {})
    r = dict(d.get("redirect") or {})
    local = _redirect_contacts()
    r["email"] = list(local.get("email") or [])
    r["sms"] = [str(x) for x in (local.get("sms") or [])]
    # No contacts means no redirect, whatever the flag says. A redirect with an
    # empty pool would silently fall through to messaging the real customer,
    # which is the one outcome the flag exists to prevent.
    r["enabled"] = bool(r.get("enabled")) and bool(r["email"] or r["sms"])
    d["redirect"] = r
    d.setdefault("enabled", False)
    d.setdefault("sms", False)
    d.setdefault("email", False)
    d.setdefault("cost_paise", {"sms": 20, "email": 2})
    return d


def delivery_cost(channel: str, cfg: dict | None = None) -> int:
    """What one message on one delivery channel costs."""
    return int((delivery(cfg)["cost_paise"] or {}).get(channel, 0))


def cost_of(steps_channels: list[str], cfg: dict | None = None) -> int:
    cfg = cfg or load_workflows()
    return sum(get(c, cfg).cost_paise for c in steps_channels)
