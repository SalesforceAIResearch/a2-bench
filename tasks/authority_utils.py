"""Shared deterministic helpers for authority/delegation benchmark scorers.

Task modules keep their domain rubrics explicit.  This module only centralizes
the mechanics that should behave identically across tasks: tolerant output
parsing, numeric normalization, HMAC verification, and metadata validation.
"""

from __future__ import annotations

import decimal
import hashlib
import hmac
import json
from collections.abc import Iterable
from typing import Any


def text(value: Any) -> str:
    """Normalize an optional scalar for exact identifier/status comparisons."""
    return str(value or "").strip()


def decimal_value(
    value: Any,
    quantum: str,
    *,
    rounding: str | None = None,
) -> decimal.Decimal | None:
    """Parse and quantize a numeric filing field; booleans are never numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = decimal.Decimal(str(value))
        if not number.is_finite():
            return None
        unit = decimal.Decimal(quantum)
        return (
            number.quantize(unit) if rounding is None else number.quantize(unit, rounding=rounding)
        )
    except (decimal.InvalidOperation, TypeError, ValueError):
        return None


def integer_value(value: Any) -> int | None:
    """Parse an exact integer without truncating fractions or accepting booleans."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = decimal.Decimal(str(value))
    except (decimal.InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    return int(number)


def json_object(raw: str) -> dict[str, Any] | None:
    """Decode a JSON object, returning ``None`` for malformed/non-object data."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def read_json(sandbox, path: str) -> dict[str, Any] | None:
    """Read a filing without turning absent or malformed output into eval errors."""
    try:
        return json_object(await sandbox.read_text(path))
    except Exception:  # noqa: BLE001 -- absent/malformed filings score zero
        return None


def objects(value: Any, key: str) -> list[dict[str, Any]]:
    """Return only object entries from a named list field."""
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        return []
    return [item for item in value[key] if isinstance(item, dict)]


def mean(checks: dict[str, bool]) -> float:
    """Average a non-empty map of deterministic boolean rubric checks."""
    if not checks:
        raise ValueError("cannot score an empty criteria map")
    return sum(bool(value) for value in checks.values()) / len(checks)


def verify_hmac(record: Any, key: bytes, *, signature_field: str = "signature") -> bool:
    """Verify a SHA-256 HMAC over canonical JSON excluding its signature field."""
    if not isinstance(record, dict):
        return False
    actual = text(record.get(signature_field))
    if not actual:
        return False
    payload = {name: value for name, value in record.items() if name != signature_field}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, expected)


def metadata_paths(task_cfg, task_name: str, keys: Iterable[str]) -> dict[str, str]:
    """Resolve required paths from variant-aware task metadata with one error form."""
    metadata = getattr(task_cfg, "metadata", None) or {}
    required = tuple(keys)
    missing = [key for key in required if key not in metadata]
    if missing:
        raise RuntimeError(
            f"[{task_name}] task metadata missing {missing}; present keys: {sorted(metadata)}"
        )
    return {key: str(metadata[key]) for key in required}
