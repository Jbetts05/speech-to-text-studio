"""Invented audio and references; never fetch or embed upstream dataset bytes."""

from __future__ import annotations

import io
import tarfile
from array import array
from collections import defaultdict
from pathlib import Path

import numpy as np
import soundfile

from ai_bench.hashing import content_digest
from benchmark_packs.speech_to_text.data_contracts import ARCHIVE_MD5, Inventory, InventoryEntry


def make_flac(frames: int = 160, *, sample_rate: int = 16000, channels: int = 1) -> bytes:
    stream = io.BytesIO()
    signal = (np.arange(frames * channels, dtype=np.int16) % 101).reshape(frames, channels)
    soundfile.write(stream, signal, sample_rate, format="FLAC", subtype="PCM_16")
    return stream.getvalue()


def make_audio(
    extension: str, frames: int = 4800, *, sample_rate: int = 16000, channels: int = 1
) -> bytes:
    stream = io.BytesIO()
    signal = array("h", (index % 101 for index in range(frames * channels)))
    container, subtype = {
        ".flac": ("FLAC", "PCM_16"),
        ".wav": ("WAV", "PCM_16"),
        ".mp3": ("MP3", "MPEG_LAYER_III"),
    }[extension]
    with soundfile.SoundFile(
        stream,
        mode="w",
        samplerate=sample_rate,
        channels=channels,
        format=container,
        subtype=subtype,
    ) as output:
        output.buffer_write(signal.tobytes(), dtype="int16")
    return stream.getvalue()


def make_inventory() -> Inventory:
    return Inventory(
        archive_md5=ARCHIVE_MD5,
        archive_sha256=content_digest(b"invented archive"),
        entries=tuple(
            sorted(
                (
                    InventoryEntry(
                        id=f"{index % 40 + 1}-{index % 3 + 1}-{index:04d}",
                        speaker=str(index % 40 + 1),
                        chapter=str(index % 3 + 1),
                        frames=160 + index,
                        input_hash=content_digest(f"input-{index}".encode()),
                        input_bytes=100,
                        reference_hash=content_digest(b"SYNTHETIC WORD"),
                        reference_bytes=14,
                    )
                    for index in range(2620)
                ),
                key=lambda entry: entry.id,
            )
        ),
    )


def add_tar_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    archive.addfile(info, io.BytesIO(content))


def make_archive(path: Path, *, count: int = 2620) -> Path:
    references: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    with tarfile.open(path, "w:gz") as archive:
        for index in range(count):
            speaker, chapter = str(index % 40 + 1), str(index % 3 + 1)
            identifier = f"{speaker}-{chapter}-{index:04d}"
            add_tar_bytes(
                archive,
                f"LibriSpeech/test-clean/{speaker}/{chapter}/{identifier}.flac",
                make_flac(160 + index),
            )
            references[(speaker, chapter)].append(f"{identifier} SYNTHETIC WORD")
        for (speaker, chapter), lines in sorted(references.items()):
            add_tar_bytes(
                archive,
                f"LibriSpeech/test-clean/{speaker}/{chapter}/{speaker}-{chapter}.trans.txt",
                ("\n".join(lines) + "\n").encode("ascii"),
            )
    return path
