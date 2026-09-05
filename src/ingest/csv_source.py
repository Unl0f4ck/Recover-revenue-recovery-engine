"""CSV ingest. Run the engine on a book we have no API access to.

recoup accepts failed payments three ways -- REST, gateway webhooks, and a CSV
upload -- and the CSV path is the one that lets a merchant try the thing before
granting anyone credentials. That is worth having for exactly the same reason
here: the classifier, the sequencer and every bound are already source-agnostic,
so the only thing standing between this engine and someone else's book is a
reader.

WHAT IT WILL AND WILL NOT DO. It produces the same `RevenueAtRisk` records the
API path produces, so everything downstream is identical. It does NOT let a CSV
name an action, a schedule or a recovery outcome -- those are the system's
decisions, and a spreadsheet that could set them would be a way to smuggle a
result past the policy layer.

A row that cannot be read is REPORTED, not skipped. A silent drop in an ingest
path is how a merchant concludes their recovery rate is poor when in fact a
third of their failures never entered the system.
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .razorpay_source import RevenueAtRisk

IST = timezone(timedelta(hours=5, minutes=30))

# Column aliases. Every export names these differently, and requiring one exact
# header is how a useful tool becomes an unusable one.
ALIASES = {
    "reference": ["reference", "payment_id", "id", "transaction_id", "txn_id",
                  "order_id", "invoice_id"],
    "amount_paise": ["amount_paise", "amount", "amount_in_paise", "value"],
    "created_at": ["created_at", "failed_at", "date", "timestamp", "created"],
    "reason": ["reason", "error_reason", "decline_reason", "failure_reason",
               "error_code"],
    "method": ["method", "payment_method", "instrument"],
    "segment": ["segment", "bank", "issuer", "issuer_bank"],
    "customer_ref": ["customer_ref", "email", "customer_email", "contact",
                     "phone", "mobile"],
    "kind": ["kind", "type", "stream"],
}


@dataclass
class CsvResult:
    items: list[RevenueAtRisk] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    amount_unit: str = "paise"

    @property
    def at_risk_paise(self) -> int:
        return sum(i.amount_paise for i in self.items)


def _pick(row: dict, field_name: str) -> str | None:
    for alias in ALIASES[field_name]:
        for key, val in row.items():
            if key and key.strip().lower().replace(" ", "_") == alias:
                v = (val or "").strip()
                if v:
                    return v
    return None


def _parse_when(raw: str | None, default: datetime) -> datetime:
    if not raw:
        return default
    if raw.isdigit():                     # unix seconds
        return datetime.fromtimestamp(int(raw), IST)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime(raw[:26], fmt)
            return d if d.tzinfo else d.replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {raw!r}")


def _parse_amount(raw: str | None, unit: str) -> int:
    if not raw:
        raise ValueError("missing amount")
    v = raw.replace(",", "").replace("₹", "").replace("Rs", "").strip()
    try:
        n = Decimal(v)
    except InvalidOperation:
        raise ValueError("amount must be a finite decimal number") from None
    # `unit` is explicit and never guessed. Inferring paise-vs-rupees from
    # magnitude gets it wrong on exactly the rows where it matters -- a genuine
    # Rs 45,000 failure and a 45000-paise one look identical -- and a
    # hundredfold error in exposure would drive every gate the wrong way.
    scaled = n if unit == "paise" else n * 100
    if not scaled.is_finite() or scaled <= 0 or scaled > 100_000_000_000:
        raise ValueError("amount must be positive and at most 100000000000 paise")
    if scaled != scaled.to_integral_value():
        raise ValueError("amount must resolve to whole paise; no rounding is applied")
    return int(scaled)


def read_csv(path: Path | str, amount_unit: str = "paise",
             now: datetime | None = None,
             default_kind: str = "payment_failure") -> CsvResult:
    """Read a merchant's exported failures into the engine's own shape."""
    if amount_unit not in ("paise", "rupees"):
        raise ValueError("amount_unit must be 'paise' or 'rupees'")
    now = now or datetime.now(IST)
    res = CsvResult(amount_unit=amount_unit)
    seen = set()

    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):   # 1 is the header
            try:
                ref = _pick(row, "reference")
                if not ref:
                    raise ValueError("no reference column found")
                if len(ref) > 200 or any(c in ref for c in ("/", "\\", "?", "#")):
                    raise ValueError("reference must be a simple identifier, not a path or URL")
                if ref in seen:
                    raise ValueError("duplicate reference in this import")
                kind = _pick(row, "kind") or default_kind
                if kind not in ("payment_failure", "checkout_abandoned", "overdue_receivable", "subscription_failure"):
                    raise ValueError("unsupported revenue stream")
                res.items.append(RevenueAtRisk(
                    kind=kind,
                    reference=ref,
                    created_at=_parse_when(_pick(row, "created_at"), now),
                    amount_paise=_parse_amount(_pick(row, "amount_paise"),
                                               amount_unit),
                    segment=_pick(row, "segment") or "UNATTRIBUTED",
                    method=_pick(row, "method"),
                    detail={"error_reason": _pick(row, "reason"),
                            "customer_ref": _pick(row, "customer_ref"),
                            "source": "csv"},
                ))
                seen.add(ref)
            except Exception as e:                        # noqa: BLE001
                # Reported, never silently dropped. A merchant whose export
                # loses a third of its rows to a date format should be told,
                # not left to conclude that recovery does not work for them.
                res.rejected.append({"line": n, "error": str(e),
                                     "row": {k: v for k, v in list(row.items())[:6]}})
    return res
