"""Explicit acquisition, validation, selection, and atomic external publication."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import urllib.request
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import IO

from pydantic import ValidationError

from ai_bench.artifacts import FileArtifactStore
from ai_bench.contracts import (
    AudioArtifact,
    BenchmarkSample,
    DatasetAvailability,
    DatasetProvenance,
    DatasetRevision,
    JsonValue,
    Modality,
    ModalityArtifact,
    TextArtifact,
)
from ai_bench.hashing import canonical_json, content_digest, sha256_digest
from ai_bench.packs import DatasetUnavailableError

from .data_contracts import (
    ARCHIVE_MD5,
    ARCHIVE_URL,
    ATTRIBUTION,
    SOURCE_URL,
    UTTERANCES,
    Inventory,
    InventoryEntry,
    PilotSelection,
    Publication,
    PublishedSamples,
)
from .selection import select_pilot

MAX_MEMBERS = 4000
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPANSION_RATIO = 100
ENV_REVISION = "AI_BENCH_SPEECH_DATASET_REVISION"
_AUDIO = re.compile(r"LibriSpeech/test-clean/([0-9]+)/([0-9]+)/([0-9]+-[0-9]+-[0-9]+)\.flac")
_REFERENCES = re.compile(r"LibriSpeech/test-clean/([0-9]+)/([0-9]+)/([0-9]+)-([0-9]+)\.trans\.txt")
_METADATA = {
    "LibriSpeech/BOOKS.TXT",
    "LibriSpeech/CHAPTERS.TXT",
    "LibriSpeech/LICENSE.TXT",
    "LibriSpeech/README.TXT",
    "LibriSpeech/SPEAKERS.TXT",
}


class DatasetValidationError(ValueError):
    pass


def require_external(path: Path) -> Path:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.absolute().parents):
        raise DatasetValidationError("External data paths must not contain links.")
    resolved = path.resolve()
    roots = [Path(__file__).resolve().parents[2]]
    roots.extend(
        parent for parent in (Path.cwd(), *Path.cwd().parents) if (parent / ".git").exists()
    )
    if any(resolved == root or root in resolved.parents for root in roots):
        raise DatasetValidationError(
            "Dataset archives and publications must be outside the repository."
        )
    return resolved


def _hash_file(path: Path) -> tuple[str, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def acquire_archive(destination: Path) -> Path:
    """Network acquisition is opt-in and never performed by pack discovery."""
    destination = require_external(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("Archive destination already exists; it will not be overwritten.")
    with tempfile.TemporaryDirectory(
        prefix=".speech-acquire-", dir=destination.parent
    ) as temporary:
        candidate = Path(temporary) / "download"
        with (
            urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response,
            candidate.open("xb") as output,
        ):
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise DatasetValidationError("Official archive exceeds compressed size limit.")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if _hash_file(candidate)[0] != ARCHIVE_MD5:
            raise DatasetValidationError("Official archive checksum mismatch; nothing published.")
        os.link(candidate, destination)
    return destination


def flac_metadata(content: bytes, *, decode: bool = False) -> int:
    if len(content) < 42 or content[:4] != b"fLaC":
        raise DatasetValidationError("Invalid FLAC signature or truncated metadata.")
    position = 4
    frames = 0
    pcm_md5 = b""
    block_index = 0
    while True:
        if position + 4 > len(content):
            raise DatasetValidationError("Truncated FLAC metadata block.")
        header = content[position]
        kind = header & 0x7F
        length = int.from_bytes(content[position + 1 : position + 4], "big")
        position += 4
        if position + length > len(content) or kind == 127:
            raise DatasetValidationError("Invalid FLAC metadata block length/type.")
        if block_index == 0:
            if kind != 0 or length != 34:
                raise DatasetValidationError("FLAC requires one leading STREAMINFO block.")
            block = content[position : position + length]
            packed = int.from_bytes(block[10:18], "big")
            sample_rate = packed >> 44
            channels = ((packed >> 41) & 7) + 1
            bits = ((packed >> 36) & 31) + 1
            frames = packed & ((1 << 36) - 1)
            pcm_md5 = block[18:34]
            if (sample_rate, channels, bits) != (16000, 1, 16) or not 0 < frames <= 960000:
                raise DatasetValidationError("FLAC must contain bounded 16 kHz mono 16-bit audio.")
            if int.from_bytes(block[:2], "big") < 16 or int.from_bytes(block[2:4], "big") < 16:
                raise DatasetValidationError("Invalid FLAC block size.")
        elif kind == 0:
            raise DatasetValidationError("Duplicate FLAC STREAMINFO.")
        position += length
        block_index += 1
        if header & 0x80:
            break
    if (
        position + 2 > len(content)
        or content[position] != 0xFF
        or content[position + 1] & 0xFC != 0xF8
    ):
        raise DatasetValidationError("FLAC contains no audio frames.")
    if decode:
        import soundfile

        try:
            with soundfile.SoundFile(io.BytesIO(content)) as audio:
                if (audio.samplerate, audio.channels, audio.frames, audio.subtype) != (
                    16000,
                    1,
                    frames,
                    "PCM_16",
                ):
                    raise DatasetValidationError("Decoded FLAC metadata differs from STREAMINFO.")
                decoded = audio.read(frames + 1, dtype="int16", always_2d=True)
                if len(decoded) != frames:
                    raise DatasetValidationError("Truncated decoded FLAC audio.")
                digest = hashlib.md5(
                    decoded.astype("<i2", copy=False).tobytes(), usedforsecurity=False
                ).digest()
                if pcm_md5 == bytes(16) or digest != pcm_md5:
                    raise DatasetValidationError(
                        "FLAC decoded PCM checksum mismatch or absent checksum."
                    )
        except soundfile.LibsndfileError as error:
            raise DatasetValidationError("FLAC audio cannot be decoded.") from error
    return frames


def _member_name(member: tarfile.TarInfo, seen: set[str]) -> str:
    name = member.name.rstrip("/") if member.isdir() else member.name
    path = PurePosixPath(name)
    parts = name.split("/")
    if (
        not name
        or "\\" in name
        or ":" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
        or any(ord(character) < 32 or ord(character) > 126 for character in name)
        or parts[0] != "LibriSpeech"
    ):
        raise DatasetValidationError("Unsafe archive member path.")
    folded = name.casefold()
    if folded in seen:
        raise DatasetValidationError("Duplicate or case-colliding archive member.")
    seen.add(folded)
    if not (member.isdir() or member.isreg()) or member.sparse is not None:
        raise DatasetValidationError(
            "Archive links, sparse files, and special members are forbidden."
        )
    if member.size < 0 or member.size > MAX_MEMBER_BYTES or (member.isdir() and member.size):
        raise DatasetValidationError("Archive member exceeds size limits.")
    return name


def _read_member(stream: IO[bytes] | None, size: int) -> bytes:
    if stream is None:
        raise DatasetValidationError("Archive member has no content.")
    content = stream.read(size + 1)
    if len(content) != size:
        raise DatasetValidationError(
            "Archive member is truncated or expanded past its declaration."
        )
    return content


def _index_archive(archive_path: Path, archive_sha256: str) -> Inventory:
    audio_entries: dict[str, tuple[str, str, int, str, int]] = {}
    references: dict[str, tuple[str, int]] = {}
    seen: set[str] = set()
    expanded = 0
    try:
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for count, member in enumerate(archive, 1):
                if count > MAX_MEMBERS:
                    raise DatasetValidationError("Archive member count exceeds limit.")
                name = _member_name(member, seen)
                expanded += member.size
                if (
                    expanded > MAX_EXPANDED_BYTES
                    or expanded > archive_path.stat().st_size * MAX_EXPANSION_RATIO
                ):
                    raise DatasetValidationError("Archive expansion exceeds limits.")
                if member.isdir():
                    if not (
                        name in {"LibriSpeech", "LibriSpeech/test-clean"}
                        or re.fullmatch(r"LibriSpeech/test-clean/[0-9]+(?:/[0-9]+)?", name)
                    ):
                        raise DatasetValidationError("Unexpected archive directory.")
                    continue
                content = _read_member(archive.extractfile(member), member.size)
                if match := _AUDIO.fullmatch(name):
                    speaker, chapter, identifier = match.groups()
                    if (
                        identifier.split("-")[:2] != [speaker, chapter]
                        or identifier in audio_entries
                    ):
                        raise DatasetValidationError("Audio identity does not match its directory.")
                    frames = flac_metadata(content, decode=True)
                    audio_entries[identifier] = (
                        speaker,
                        chapter,
                        frames,
                        content_digest(content),
                        len(content),
                    )
                elif match := _REFERENCES.fullmatch(name):
                    speaker, chapter, file_speaker, file_chapter = match.groups()
                    if (speaker, chapter) != (file_speaker, file_chapter):
                        raise DatasetValidationError(
                            "Reference filename does not match its chapter."
                        )
                    for line in content.decode("utf-8").splitlines():
                        identifier, separator, text = line.partition(" ")
                        if (
                            not separator
                            or not text.strip()
                            or not text.isprintable()
                            or not re.fullmatch(r"[0-9]+-[0-9]+-[0-9]+", identifier)
                            or identifier.split("-")[:2] != [speaker, chapter]
                            or identifier in references
                            or len(text.encode("utf-8")) > 16384
                        ):
                            raise DatasetValidationError(
                                "Invalid or duplicate transcript reference."
                            )
                        reference_bytes = text.encode("utf-8")
                        references[identifier] = (
                            content_digest(reference_bytes),
                            len(reference_bytes),
                        )
                elif name not in _METADATA:
                    raise DatasetValidationError("Unexpected file in official archive.")
    except (tarfile.TarError, EOFError, UnicodeDecodeError) as error:
        raise DatasetValidationError("Archive or reference content is corrupt.") from error
    if len(audio_entries) != UTTERANCES or set(audio_entries) != set(references):
        raise DatasetValidationError(
            "Archive must contain exactly 2620 paired FLAC/reference utterances."
        )
    return Inventory(
        archive_md5=ARCHIVE_MD5,
        archive_sha256=archive_sha256,
        entries=tuple(
            InventoryEntry(
                id=identifier,
                speaker=audio_entries[identifier][0],
                chapter=audio_entries[identifier][1],
                frames=audio_entries[identifier][2],
                input_hash=audio_entries[identifier][3],
                input_bytes=audio_entries[identifier][4],
                reference_hash=references[identifier][0],
                reference_bytes=references[identifier][1],
            )
            for identifier in sorted(audio_entries)
        ),
    )


def _store_selected(
    archive_path: Path,
    inventory: Inventory,
    selection: PilotSelection,
    store: FileArtifactStore,
) -> None:
    selected = {item.id for item in selection.selected}
    indexed = {entry.id: entry for entry in inventory.entries}
    with tarfile.open(archive_path, "r|gz") as archive:
        for member in archive:
            if match := _AUDIO.fullmatch(member.name):
                identifier = match[3]
                if identifier in selected:
                    content = _read_member(archive.extractfile(member), member.size)
                    if content_digest(content) != indexed[identifier].input_hash:
                        raise DatasetValidationError("Validated audio changed before publication.")
                    store.put_bytes(content, modality=Modality.AUDIO, media_type="audio/flac")
            elif _REFERENCES.fullmatch(member.name):
                for line in (
                    _read_member(archive.extractfile(member), member.size)
                    .decode("utf-8")
                    .splitlines()
                ):
                    identifier, _, text = line.partition(" ")
                    if identifier in selected:
                        content = text.encode("utf-8")
                        if content_digest(content) != indexed[identifier].reference_hash:
                            raise DatasetValidationError(
                                "Validated reference changed before publication."
                            )
                        store.put_bytes(content, modality=Modality.TEXT, media_type="text/plain")


def publish_archive(archive_path: Path, output_root: Path) -> Path:
    archive_path = require_external(archive_path)
    output_root = require_external(output_root)
    if archive_path.is_symlink() or not 0 < archive_path.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise DatasetValidationError("Invalid archive path or compressed size.")
    md5, sha256 = _hash_file(archive_path)
    if md5 != ARCHIVE_MD5:
        raise DatasetValidationError("Official archive checksum mismatch; nothing published.")
    output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".speech-publish-", dir=output_root) as temporary:
        frozen_archive = Path(temporary) / "archive.tar.gz"
        with archive_path.open("rb") as source, frozen_archive.open("xb") as target:
            copied = 0
            while chunk := source.read(1024 * 1024):
                copied += len(chunk)
                if copied > MAX_ARCHIVE_BYTES:
                    raise DatasetValidationError("Archive grew beyond its compressed size limit.")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if frozen_archive.stat().st_size > MAX_ARCHIVE_BYTES or _hash_file(frozen_archive) != (
            md5,
            sha256,
        ):
            raise DatasetValidationError("Archive changed while creating its validation snapshot.")
        candidate = Path(temporary) / "revision"
        store = FileArtifactStore(candidate / "artifacts")
        inventory = _index_archive(frozen_archive, sha256)
        selection = select_pilot(inventory)
        _store_selected(frozen_archive, inventory, selection, store)
        indexed = {entry.id: entry for entry in inventory.entries}
        samples: list[BenchmarkSample] = []
        for selected in selection.selected:
            entry = indexed[selected.id]
            samples.append(
                BenchmarkSample(
                    id=entry.id,
                    input=AudioArtifact(
                        id=entry.input_hash,
                        modality=Modality.AUDIO,
                        media_type="audio/flac",
                        size_bytes=entry.input_bytes,
                        uri=f"artifact://sha256/{entry.input_hash}",
                        metadata={
                            "frames": entry.frames,
                            "sample_rate": entry.sample_rate,
                            "filename": f"{entry.id}.flac",
                        },
                    ),
                    reference=TextArtifact(
                        id=entry.reference_hash,
                        modality=Modality.TEXT,
                        media_type="text/plain",
                        size_bytes=entry.reference_bytes,
                        uri=f"artifact://sha256/{entry.reference_hash}",
                        metadata={"text": store.get_bytes(entry.reference_hash).decode("utf-8")},
                    ),
                    metadata={
                        "speaker": entry.speaker,
                        "chapter": entry.chapter,
                        "rank": selected.rank,
                        "stratum": selected.stratum,
                    },
                )
            )
        snapshot = PublishedSamples(samples=tuple(samples))
        selection_hash = sha256_digest(selection)
        revision = DatasetRevision(
            id=f"librispeech-pilot-{selection_hash[:32]}",
            version="1.0.0",
            content_hash=sha256_digest(snapshot.samples),
            provenance=DatasetProvenance(source=SOURCE_URL, license="CC BY 4.0", synthetic=False),
        )
        publication = Publication(
            revision=revision,
            inventory_hash=sha256_digest(inventory),
            selection_hash=selection_hash,
            samples_hash=sha256_digest(snapshot),
        )
        for name, value in (
            ("inventory.json", inventory),
            ("selection.json", selection),
            ("samples.json", snapshot),
            ("publication.json", publication),
        ):
            with (candidate / name).open("xb") as stream:
                stream.write(canonical_json(value))
                stream.flush()
                os.fsync(stream.fileno())
        destination = output_root / selection_hash
        if destination.exists():
            existing = _load_publication(destination)
            if existing[0] != publication:
                raise DatasetValidationError(
                    "Existing immutable revision conflicts with publication."
                )
        else:
            try:
                candidate.rename(destination)
            except FileExistsError:
                if _load_publication(destination)[0] != publication:
                    raise DatasetValidationError(
                        "Concurrent publication conflicts with revision."
                    ) from None
        return destination


def _read_published(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise DatasetValidationError("Published revision has a missing, unsafe, or oversized file.")
    return path.read_bytes()


def _load_publication(path: Path) -> tuple[Publication, PublishedSamples, PilotSelection]:
    if path.is_symlink() or not path.is_dir():
        raise DatasetValidationError("Published revision directory is missing or unsafe.")
    publication = Publication.model_validate_json(_read_published(path / "publication.json", 16384))
    inventory_bytes = _read_published(path / "inventory.json", 4 * 1024 * 1024)
    selection_bytes = _read_published(path / "selection.json", 65536)
    samples_bytes = _read_published(path / "samples.json", 1024 * 1024)
    if (
        content_digest(inventory_bytes) != publication.inventory_hash
        or content_digest(selection_bytes) != publication.selection_hash
        or content_digest(samples_bytes) != publication.samples_hash
    ):
        raise DatasetValidationError("Published revision content hash mismatch.")
    inventory = Inventory.model_validate_json(inventory_bytes)
    selection = PilotSelection.model_validate_json(selection_bytes)
    snapshot = PublishedSamples.model_validate_json(samples_bytes)
    if (
        inventory.archive_md5 != ARCHIVE_MD5
        or select_pilot(inventory) != selection
        or tuple(sample.id for sample in snapshot.samples)
        != tuple(item.id for item in selection.selected)
        or publication.revision.content_hash != sha256_digest(snapshot.samples)
        or publication.revision.id != f"librispeech-pilot-{publication.selection_hash[:32]}"
    ):
        raise DatasetValidationError(
            "Published selection/revision does not match its validated inventory."
        )
    for sample, selected in zip(snapshot.samples, selection.selected, strict=True):
        if (
            sample.input.id != selected.input_hash
            or sample.reference is None
            or sample.reference.id != selected.reference_hash
            or sample.role.value != "scored"
        ):
            raise DatasetValidationError("Selected artifact identity mismatch.")
        for artifact in (sample.input, sample.reference):
            content = _read_object(path, artifact)
            if artifact is sample.input:
                if flac_metadata(content) != sample.input.metadata.get("frames"):
                    raise DatasetValidationError("Selected FLAC metadata mismatch.")
            elif content.decode("utf-8") != sample.reference.metadata.get("text"):
                raise DatasetValidationError("Selected reference metadata mismatch.")
    return publication, snapshot, selection


def _read_object(path: Path, artifact: ModalityArtifact) -> bytes:
    object_path = path / "artifacts" / "objects" / artifact.id[:2] / artifact.id
    if any(parent.is_symlink() for parent in object_path.parents if parent != path.parent):
        raise DatasetValidationError("Published artifact path contains a link.")
    content = _read_published(object_path, MAX_MEMBER_BYTES)
    if len(content) != artifact.size_bytes or content_digest(content) != artifact.id:
        raise DatasetValidationError("Published artifact hash or byte length mismatch.")
    return content


class SpeechDataset:
    id = "librispeech-test-clean-pilot-v1"

    def __init__(self, path: Path | None = None) -> None:
        configured = os.getenv(ENV_REVISION)
        self.path = path if path is not None else (Path(configured) if configured else None)

    def _load(self) -> tuple[Publication, PublishedSamples, PilotSelection]:
        if self.path is None:
            raise DatasetUnavailableError(self.availability())
        return _load_publication(require_external(self.path))

    def availability(self) -> DatasetAvailability:
        if self.path is None:
            return DatasetAvailability(
                status="not-configured",
                reason=(
                    f"Publish the external 26-sample pilot and set {ENV_REVISION} "
                    "to its revision directory."
                ),
                sample_count=26,
                scored_sample_count=26,
            )
        try:
            publication, _, _ = self._load()
        except (DatasetValidationError, OSError, ValidationError, UnicodeDecodeError):
            return DatasetAvailability(
                status="invalid",
                reason=(
                    "Configured pilot revision is incomplete or corrupt. "
                    "Revalidate and publish the official archive externally."
                ),
                sample_count=26,
                scored_sample_count=26,
            )
        return DatasetAvailability(
            status="ready",
            reason="Complete published 26-sample pilot is available.",
            revision=publication.revision,
            sample_count=26,
            scored_sample_count=26,
        )

    @property
    def revision(self) -> DatasetRevision:
        return self._load()[0].revision

    def samples(self) -> tuple[BenchmarkSample, ...]:
        return self._load()[1].samples

    def read_artifact(self, artifact: ModalityArtifact) -> bytes:
        if self.path is None:
            raise DatasetUnavailableError(self.availability())
        return _read_object(require_external(self.path), artifact)

    def snapshot_metadata(self) -> Mapping[str, JsonValue]:
        publication, _, selection = self._load()
        return {
            "publication": publication.model_dump(mode="json"),
            "selection": selection.model_dump(mode="json"),
            "source": SOURCE_URL,
            "attribution": ATTRIBUTION,
        }

    def publication_files(self) -> Mapping[str, Path]:
        """Exact-byte archival closure, independent of the upstream acquisition archive."""
        _, snapshot, _ = self._load()
        assert self.path is not None
        root = require_external(self.path)
        files = {
            name: root / name
            for name in ("publication.json", "inventory.json", "selection.json", "samples.json")
        }
        for sample in snapshot.samples:
            for artifact in (sample.input, sample.reference):
                if artifact is not None:
                    relative = PurePosixPath("artifacts", "objects", artifact.id[:2], artifact.id)
                    files[str(relative)] = root.joinpath(*relative.parts)
        return MappingProxyType(files)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acquire or publish the external LibriSpeech pilot."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    acquire = commands.add_parser("acquire", help="Explicitly download the official archive.")
    acquire.add_argument("--destination", type=Path, required=True)
    publish = commands.add_parser(
        "publish", help="Validate an archive and publish an immutable revision."
    )
    publish.add_argument("--archive", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "acquire":
        result = acquire_archive(arguments.destination)
    else:
        result = publish_archive(arguments.archive, arguments.output)
    print(json.dumps({"path": str(result)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
