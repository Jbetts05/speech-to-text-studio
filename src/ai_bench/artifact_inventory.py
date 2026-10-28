"""Bounded inventory of application-owned immutable objects and response aliases."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_DIGEST = r"[a-f0-9]{64}"
_CONTENT = re.compile(rf"^(?:objects|sha256)/([a-f0-9]{{2}})/({_DIGEST})$")
_FILE_DESCRIPTOR = re.compile(rf"^objects/([a-f0-9]{{2}})/({_DIGEST})(?:\.({_DIGEST}))?\.json$")
_BLOB_DESCRIPTOR = re.compile(rf"^descriptors/({_DIGEST})/({_DIGEST})$")
_RESPONSE = re.compile(
    rf"^receipts/({_DIGEST})/(?:generation-([0-9]{{20}})/)?"
    rf"({_DIGEST})/([0-9]{{10}})(?:\.checkpoint)?$"
)


@dataclass(frozen=True)
class InventoryEntry:
    key: str
    etag: str
    kind: Literal["object", "descriptor", "response"]
    artifact_id: str | None = None
    run_id: str | None = None
    incarnation: int = 0


@dataclass(frozen=True)
class InventoryPage:
    entries: tuple[InventoryEntry, ...]
    cursor: str | None


def parse_inventory_entry(key: str, etag: str) -> InventoryEntry | None:
    if match := _CONTENT.fullmatch(key):
        if match[1] == match[2][:2]:
            return InventoryEntry(key, etag, "object", artifact_id=match[2])
    elif match := _FILE_DESCRIPTOR.fullmatch(key):
        if match[1] == match[2][:2]:
            return InventoryEntry(key, etag, "descriptor", artifact_id=match[2])
    elif match := _BLOB_DESCRIPTOR.fullmatch(key):
        return InventoryEntry(key, etag, "descriptor", artifact_id=match[1])
    elif (match := _RESPONSE.fullmatch(key)) and 1 <= int(match[4]) <= 20:
        return InventoryEntry(
            key, etag, "response", run_id=match[1], incarnation=int(match[2] or 0)
        )
    return None
