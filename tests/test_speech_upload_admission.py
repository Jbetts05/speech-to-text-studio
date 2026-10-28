from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import soundfile

from benchmark_packs.speech_to_text.dataset import DatasetValidationError
from benchmark_packs.speech_to_text.upload import SpeechDatasetImporter


@dataclass(frozen=True)
class ReservedFile:
    name: str
    size_bytes: int
    sha256: str = "a" * 64
    media_type: str | None = None


def pair(stem: str = "synthetic") -> tuple[ReservedFile, ReservedFile]:
    return (
        ReservedFile(f"{stem}.wav", 25000, media_type="audio/wav"),
        ReservedFile(f"{stem}.txt", 20, media_type="text/plain"),
    )


def test_admission_is_pure_and_does_not_require_a_staging_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Admission must not perform I/O, decode, or request credentials.")

    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(soundfile, "SoundFile", forbidden)
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", forbidden)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", forbidden)
    SpeechDatasetImporter().validate_admission(pair())


@pytest.mark.parametrize(
    "name",
    [
        "../synthetic.wav",
        "/synthetic.wav",
        "a\\synthetic.wav",
        "C:synthetic.wav",
        "CON.wav",
        "synthetic\u202e.wav",
        "synthetic\x00.wav",
        "synthetic.wav ",
        "synthetic.zip",
        "synthetic.m3u",
        "https://example.invalid/synthetic.wav",
    ],
)
def test_unsafe_filename_rejected_before_admission(name: str) -> None:
    audio, reference = pair()
    with pytest.raises(DatasetValidationError):
        SpeechDatasetImporter().validate_admission((replace(audio, name=name), reference))


@pytest.mark.parametrize(
    "names",
    [
        ("same.wav", "same.wav"),
        ("same.wav", "SAME.txt"),
        ("Café.wav", "Cafe\u0301.txt"),
        ("\uff21.wav", "A.txt"),
        ("same.wav", "other.txt"),
        ("same.wav", "same.mp3"),
    ],
)
def test_exact_pairing_and_collisions_rejected_before_admission(names: tuple[str, str]) -> None:
    audio, reference = pair()
    with pytest.raises(DatasetValidationError):
        SpeechDatasetImporter().validate_admission(
            (replace(audio, name=names[0]), replace(reference, name=names[1]))
        )


@pytest.mark.parametrize("count", [0, 1, 3, 22])
def test_pair_count_rejected_before_admission(count: int) -> None:
    with pytest.raises(DatasetValidationError):
        SpeechDatasetImporter().validate_admission(tuple(pair()[0] for _ in range(count)))


@pytest.mark.parametrize(
    ("role", "changes"),
    [
        (0, {"size_bytes": 25_000_001}),
        (1, {"size_bytes": 16_385}),
        (0, {"size_bytes": 0}),
        (0, {"size_bytes": True}),
        (0, {"size_bytes": 100.0}),
        (0, {"sha256": "not-a-hash"}),
        (0, {"media_type": "text/plain"}),
        (1, {"media_type": "audio/wav"}),
        (1, {"media_type": "text/plain; charset=latin-1"}),
    ],
)
def test_role_size_hash_and_mime_rejected_before_admission(
    role: int, changes: dict[str, Any]
) -> None:
    files = list(pair())
    files[role] = replace(files[role], **changes)
    with pytest.raises(DatasetValidationError):
        SpeechDatasetImporter().validate_admission(tuple(files))


def test_twenty_files_and_exact_aggregate_byte_boundary_without_data() -> None:
    files = tuple(
        file
        for index in range(10)
        for file in (
            ReservedFile(f"case-{index}.flac", 24_983_616, media_type="audio/flac"),
            ReservedFile(f"case-{index}.txt", 16_384, media_type="text/plain"),
        )
    )
    assert sum(file.size_bytes for file in files) == 250_000_000
    SpeechDatasetImporter().validate_admission(files)
    overflow = (replace(files[0], size_bytes=files[0].size_bytes + 1), *files[1:])
    with pytest.raises(DatasetValidationError, match="aggregate"):
        SpeechDatasetImporter().validate_admission(overflow)
