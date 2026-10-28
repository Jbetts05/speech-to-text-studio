"""Pack-owned English audio/reference import; storage and publication stay in core."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from ai_bench.contracts import (
    AudioArtifact,
    BenchmarkSample,
    DatasetProvenance,
    DatasetRevision,
    JsonValue,
    Modality,
    TextArtifact,
)
from ai_bench.dataset_imports import DatasetImportFile, DatasetImportResult
from ai_bench.hashing import sha256_digest

from .dataset import DatasetValidationError
from .evaluation import NORMALIZATION_VERSION
from .upload_media import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SECONDS,
    MAX_DATASET_PCM_BYTES,
    MAX_DATASET_SECONDS,
    MAX_FILES,
    MAX_PAIRS,
    MAX_REFERENCE_BYTES,
    MAX_TOTAL_BYTES,
    MEDIA_TYPES,
    validate_audio,
    validate_filename,
    validate_mime,
    validate_reference,
)

IMPORT_VERSION = "speech-upload-v1"


class AdmissionFile(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    @property
    def media_type(self) -> str | None: ...


def _admission_pairs[File: AdmissionFile](
    files: Sequence[File],
) -> dict[str, dict[str, tuple[File, str]]]:
    if not 2 <= len(files) <= MAX_FILES or len(files) % 2:
        raise DatasetValidationError("Upload requires 1-10 pairs and at most 20 files.")
    if any(type(file.size_bytes) is not int or file.size_bytes <= 0 for file in files):
        raise DatasetValidationError("Upload byte counts must be positive integers.")
    if sum(file.size_bytes for file in files) > MAX_TOTAL_BYTES:
        raise DatasetValidationError("Upload aggregate byte count exceeds the dataset limit.")
    names: set[str] = set()
    stems: dict[str, str] = {}
    pairs: dict[str, dict[str, tuple[File, str]]] = {}
    for file in files:
        if re.fullmatch(r"[a-f0-9]{64}", file.sha256) is None:
            raise DatasetValidationError("Upload files require their exact SHA-256 identity.")
        stem, extension = validate_filename(file.name)
        name_key = unicodedata.normalize("NFKC", file.name).casefold()
        stem_key = unicodedata.normalize("NFKC", stem).casefold()
        if name_key in names or (stem_key in stems and stems[stem_key] != stem):
            raise DatasetValidationError("Duplicate, case or Unicode-colliding upload name.")
        names.add(name_key)
        stems[stem_key] = stem
        role = "reference" if extension == ".txt" else "audio"
        if role in pairs.setdefault(stem, {}):
            raise DatasetValidationError("Each exact basename must have one audio and one TXT.")
        validate_mime(extension, file.media_type)
        limit = MAX_REFERENCE_BYTES if role == "reference" else MAX_AUDIO_BYTES
        if not 0 < file.size_bytes <= limit:
            raise DatasetValidationError("Upload file exceeds its role's byte limit.")
        pairs[stem][role] = file, extension
    if not 1 <= len(pairs) <= MAX_PAIRS or any(
        set(pair) != {"audio", "reference"} for pair in pairs.values()
    ):
        raise DatasetValidationError("Every audio file requires its exact basename TXT pair.")
    return pairs


def _read_file(file: DatasetImportFile, limit: int) -> bytes:
    path = file.path
    if any(item.is_symlink() or item.is_junction() for item in (path, *path.absolute().parents)):
        raise DatasetValidationError("Upload staging paths cannot contain links.")
    before = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size != file.size_bytes
        or not 0 < file.size_bytes <= limit
    ):
        raise DatasetValidationError("Upload file is not regular or has an invalid byte count.")
    digest = hashlib.sha256()
    content = bytearray()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise DatasetValidationError("Upload file changed while being opened.")
        while chunk := stream.read(min(1024 * 1024, limit + 1 - len(content))):
            content.extend(chunk)
            digest.update(chunk)
            if len(content) > file.size_bytes:
                raise DatasetValidationError("Upload exceeds its reserved byte count.")
        after = os.fstat(stream.fileno())
    if (
        len(content) != file.size_bytes
        or digest.hexdigest() != file.sha256
        or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
    ):
        raise DatasetValidationError(
            "Upload byte length, SHA-256 or immutable file identity differs."
        )
    return bytes(content)


class SpeechDatasetImporter:
    @property
    def descriptor(self) -> Mapping[str, JsonValue]:
        return {
            "id": "speech-audio-reference",
            "version": IMPORT_VERSION,
            "title": "English audio and reference pairs",
            "input_extensions": list(MEDIA_TYPES),
            "reference_extensions": [".txt"],
            "max_files": MAX_FILES,
            "max_input_bytes": MAX_AUDIO_BYTES,
            "max_reference_bytes": MAX_REFERENCE_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES,
            "language": "en",
            "instructions": [
                "Select 1-10 exact, case-sensitive basename audio/TXT pairs.",
                "Original FLAC, WAV or MP3; strict UTF-8 references; English declared by uploader.",
                "Mono/stereo, 8-48 kHz; at most 300 seconds each and 1800 seconds total.",
                "No archives, directories, URLs or playlists. Original bytes are never converted.",
                "Decoded PCM is limited to 128 MiB per file and 768 MiB per dataset.",
            ],
        }

    def import_files(self, files: tuple[DatasetImportFile, ...]) -> DatasetImportResult:
        from .decode_process import import_in_decoder

        self.validate_admission(files)
        return import_in_decoder(files)

    def validate_admission(self, files: Sequence[AdmissionFile]) -> None:
        """Validate reserved metadata only, before any body reception or file/data I/O."""
        _admission_pairs(files)

    def _import_files(self, files: tuple[DatasetImportFile, ...]) -> DatasetImportResult:
        pairs = _admission_pairs(files)
        samples: list[BenchmarkSample] = []
        paths: dict[str, Path] = {}
        seconds = Fraction(0)
        pcm_bytes = 0
        for index, stem in enumerate(sorted(pairs)):
            source, extension = pairs[stem]["audio"]
            reference, _ = pairs[stem]["reference"]
            text = validate_reference(_read_file(reference, MAX_REFERENCE_BYTES))
            audio = validate_audio(
                _read_file(source, MAX_AUDIO_BYTES), extension, source.media_type
            )
            seconds += Fraction(audio.frames, audio.sample_rate)
            pcm_bytes += audio.decoded_pcm_bytes
            if seconds > MAX_DATASET_SECONDS or pcm_bytes > MAX_DATASET_PCM_BYTES:
                raise DatasetValidationError("Dataset duration or decoded PCM exceeds its limit.")
            sample_hash = sha256_digest(
                {"input": source.sha256, "reference": reference.sha256, "ordinal": index}
            )
            samples.append(
                BenchmarkSample(
                    id=f"upload-{sample_hash[:40]}",
                    input=AudioArtifact(
                        id=source.sha256,
                        modality=Modality.AUDIO,
                        media_type=audio.media_type,
                        size_bytes=source.size_bytes,
                        uri=f"artifact://sha256/{source.sha256}",
                        metadata={
                            "frames": audio.frames,
                            "sample_rate": audio.sample_rate,
                            "channels": audio.channels,
                            "duration_seconds": str(audio.duration_seconds),
                            "validation_version": IMPORT_VERSION,
                        },
                    ),
                    reference=TextArtifact(
                        id=reference.sha256,
                        modality=Modality.TEXT,
                        media_type="text/plain",
                        size_bytes=reference.size_bytes,
                        uri=f"artifact://sha256/{reference.sha256}",
                        metadata={"text": text},
                    ),
                    metadata={"language": "en", "language_declaration": "uploader"},
                )
            )
            paths[source.sha256] = source.path
            paths[reference.sha256] = reference.path
        snapshot = tuple(samples)
        content_hash = sha256_digest(snapshot)
        return DatasetImportResult(
            revision=DatasetRevision(
                id=f"speech-upload-{content_hash[:40]}",
                version=IMPORT_VERSION,
                content_hash=content_hash,
                provenance=DatasetProvenance(
                    source="Public user-supplied audio and reference upload",
                    license="Uploader declares rights to share; not independently verified",
                    synthetic=False,
                ),
            ),
            samples=snapshot,
            metadata=MappingProxyType(
                {
                    "importer": IMPORT_VERSION,
                    "normalization_version": NORMALIZATION_VERSION,
                    "language": "en",
                    "language_declaration": "uploader",
                    "duration_seconds": str(Decimal(seconds.numerator) / seconds.denominator),
                    "decoded_pcm_bytes": pcm_bytes,
                    "max_audio_seconds": MAX_AUDIO_SECONDS,
                    "original_bytes_preserved": True,
                }
            ),
            files=MappingProxyType(paths),
        )
