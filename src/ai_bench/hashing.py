"""Canonical JSON and stable identity helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _canonical(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="json", exclude_none=False))
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"$bytes_sha256": hashlib.sha256(value).hexdigest(), "$size": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Non-finite floats are not canonical JSON")
        return value
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Serialize a value deterministically as UTF-8 JSON."""
    return json.dumps(
        _canonical(value),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def content_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def normalize_media_type(value: str) -> str:
    parts = [part.strip() for part in value.split(";")]
    base = parts[0].casefold()
    parameters = []
    for part in parts[1:]:
        if not part:
            continue
        name, separator, parameter_value = part.partition("=")
        parameters.append(f"{name.strip().casefold()}{separator}{parameter_value.strip()}")
    return ";".join((base, *parameters))


def stable_task_id(run_id: str, sample_id: str, solution_id: str) -> str:
    return sha256_digest({"run_id": run_id, "sample_id": sample_id, "solution_id": solution_id})
