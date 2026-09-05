"""Per-merchant configuration.

recoup and getpaidhq both key retry schedules, templates and credentials by
merchant. The honest version of that here is narrower than theirs and says so:
this is a **configuration resolver**, not multi-tenancy.

WHAT IT DOES. Lets a second merchant exist with a different schedule, a
different contact ceiling, a different gateway or different quiet hours,
resolved by overlaying their block onto the defaults. Everything not overridden
comes from `declines.yaml`, so a merchant config is a diff rather than a copy --
which matters because a copied config drifts from the default silently and
nobody notices until a bound stops matching the one in the README.

WHAT IT DOES NOT DO. It does not isolate DATA. All merchants share one ledger,
one suppression list and one lock. Real multi-tenancy means a merchant id on
every row and every query scoped by it, and claiming that on the strength of a
config overlay would be exactly the sort of thing this project has spent its
time not doing. Ledger paths are per-merchant so a demo does not mix books, but
that is filesystem hygiene, not isolation.

Everything runs as merchant `default` unless told otherwise, and that merchant
is the live Razorpay test account.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..yamlcache import load_yaml

CONFIG = Path(__file__).resolve().parents[2] / "config"
MERCHANTS = CONFIG / "merchants.yaml"
DATA = Path("data/live")

DEFAULT = "default"


@dataclass(frozen=True)
class Merchant:
    id: str
    name: str
    gateway: str = "razorpay"
    cfg: dict = field(default_factory=dict)          # merged declines config
    env_prefix: str = ""                             # RAZORPAY_ vs ACME_RAZORPAY_
    base_url: str = ""                               # for signed customer links
    notify: dict = field(default_factory=dict)       # {"sms": bool, "email": bool}
    source: str = "razorpay"                         # where its cases come from

    # Per-merchant ledgers. Filesystem hygiene so a demo does not mix books --
    # NOT data isolation, which would need a merchant id on every row.
    @property
    def ledger(self) -> Path:
        return DATA / self._f("dunning_ledger.jsonl")

    @property
    def suppression(self) -> Path:
        return DATA / self._f("suppression.jsonl")

    @property
    def notifications(self) -> Path:
        return DATA / self._f("notifications.jsonl")

    @property
    def review(self) -> Path:
        return DATA / self._f("review.jsonl")

    @property
    def webhooks(self) -> Path:
        return DATA / self._f("webhook_events.jsonl")

    @property
    def promise(self) -> Path:
        return DATA / self._f("promises.jsonl")

    def _f(self, name: str) -> str:
        return name if self.id == DEFAULT else f"{self.id}.{name}"

    def paths(self) -> dict:
        """The keyword arguments every campaign entry point wants."""
        return {"path": self.ledger, "sup_path": self.suppression,
                "notif_path": self.notifications}


def _merge(base: dict, over: dict) -> dict:
    """Deep overlay. A merchant config is a DIFF, never a copy.

    Copying the whole config per merchant is the obvious alternative and it
    rots: the default changes, the copies do not, and a bound quietly stops
    matching the one documented in the README.
    """
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_registry(path: Path | None = None) -> dict:
    p = path or MERCHANTS
    if not p.exists():
        return {}
    return load_yaml(p) or {}


def get(merchant_id: str = DEFAULT, base_cfg: dict | None = None,
        registry_path: Path | None = None) -> Merchant:
    """Resolve one merchant's effective configuration."""
    from .declines import load_declines
    base = base_cfg or load_declines()
    reg = load_registry(registry_path)
    entry = (reg.get("merchants") or {}).get(merchant_id)

    if entry is None:
        if merchant_id != DEFAULT:
            raise KeyError(f"no merchant {merchant_id!r} in {MERCHANTS.name}; "
                           f"have {sorted((reg.get('merchants') or {}))}")
        entry = {}

    return Merchant(
        id=merchant_id,
        name=entry.get("name", "Razorpay test account"),
        gateway=entry.get("gateway", "razorpay"),
        cfg=_merge(base, entry.get("overrides") or {}),
        env_prefix=entry.get("env_prefix", ""),
        base_url=entry.get("base_url", ""),
        notify={"sms": False, "email": False, **(entry.get("notify") or {})},
        source=entry.get("source", "razorpay"),
    )


def known(registry_path: Path | None = None) -> list[str]:
    reg = load_registry(registry_path)
    return sorted({DEFAULT, *(reg.get("merchants") or {})})
