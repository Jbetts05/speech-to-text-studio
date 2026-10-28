"""Shared rejection policy for external data and local provider configuration."""

from __future__ import annotations

from pathlib import PurePosixPath

EXTERNAL_SUFFIXES = frozenset(
    {
        ".flac",
        ".wav",
        ".mp3",
        ".tar",
        ".gz",
        ".tgz",
        ".zip",
        ".7z",
        ".bz2",
        ".xz",
    }
)
EXTERNAL_FILENAMES = frozenset(
    {
        "inventory.json",
        "selection.json",
        "publication.json",
        "samples.json",
        "provider-config.json",
        "speech-provider-config.json",
        "external-manifest.json",
    }
)


def external_payload_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/").lower())
    return (
        path.suffix in EXTERNAL_SUFFIXES
        or path.name in EXTERNAL_FILENAMES
        or path.name.endswith(".trans.txt")
        or path.name.startswith(("provider-config.", "speech-provider-config."))
        or any(part in {"external-data", "dataset-cache", "librispeech"} for part in path.parts)
    )
