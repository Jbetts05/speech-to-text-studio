from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections import Counter
from pathlib import Path

import pytest

from ai_bench.hashing import canonical_json, sha256_digest
from ai_bench.packs import DatasetUnavailableError
from benchmark_packs.speech_to_text import dataset
from benchmark_packs.speech_to_text.data_contracts import Inventory, PublishedSamples
from benchmark_packs.speech_to_text.dataset import (
    DatasetValidationError,
    SpeechDataset,
    _index_archive,
    _member_name,
    flac_metadata,
    publish_archive,
)
from benchmark_packs.speech_to_text.selection import select_pilot
from tests.speech_support import add_tar_bytes, make_archive, make_flac, make_inventory


def test_selection_is_exact_anchored_stratified_and_reproducible() -> None:
    inventory = make_inventory()
    selection = select_pilot(inventory)
    assert selection == select_pilot(Inventory.model_validate_json(canonical_json(inventory)))
    assert len({item.id for item in selection.selected}) == 26
    assert [item.stratum for item in selection.selected] == list(range(26))
    assert {0, 1309, 2619}.issubset({item.rank for item in selection.selected})
    assert selection.inventory_hash == sha256_digest(inventory)
    for item in selection.selected:
        assert item.stratum * 2620 // 26 <= item.rank < (item.stratum + 1) * 2620 // 26

    ordered = sorted(inventory.entries, key=lambda item: (item.duration, item.id))
    speakers = Counter(ordered[rank].speaker for rank in (0, 1309, 2619))
    chapters = Counter((ordered[rank].speaker, ordered[rank].chapter) for rank in (0, 1309, 2619))
    for item in selection.selected:
        if item.rank in selection.anchor_ranks:
            continue
        candidates = ordered[item.stratum * 2620 // 26 : (item.stratum + 1) * 2620 // 26]
        minimum = min(speakers[entry.speaker] for entry in candidates)
        selected = ordered[item.rank]
        assert speakers[selected.speaker] == minimum
        assert chapters[(selected.speaker, selected.chapter)] == min(
            chapters[(entry.speaker, entry.chapter)]
            for entry in candidates
            if speakers[entry.speaker] == minimum
        )
        speakers[selected.speaker] += 1
        chapters[(selected.speaker, selected.chapter)] += 1


def test_selection_ties_use_exact_duration_then_identifier() -> None:
    inventory = make_inventory()
    tied = inventory.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"frames": 1000}) for entry in inventory.entries
            )
        }
    )
    selection = select_pilot(tied)
    assert selection.selected[0].id == inventory.entries[0].id
    assert selection.selected[-1].id == inventory.entries[-1].id
    changed_references = tied.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"reference_hash": "f" * 64}) for entry in tied.entries
            )
        }
    )
    assert [item.id for item in select_pilot(changed_references).selected] == [
        item.id for item in selection.selected
    ]


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/absolute",
        "LibriSpeech/../escape",
        "LibriSpeech//empty",
        "LibriSpeech/./relative",
        "LibriSpeech\\escape",
        "LibriSpeech/trailing.",
        "LibriSpeech/trailing ",
        "LibriSpeech/a\x00b",
    ],
)
def test_archive_paths_are_fail_closed(name: str) -> None:
    with pytest.raises(DatasetValidationError, match="Unsafe"):
        _member_name(tarfile.TarInfo(name), set())


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE]
)
def test_archive_rejects_links_and_special_files(kind: bytes) -> None:
    info = tarfile.TarInfo("LibriSpeech/unsafe")
    info.type = kind
    with pytest.raises(DatasetValidationError, match="forbidden"):
        _member_name(info, set())


def test_archive_rejects_duplicate_and_case_colliding_names() -> None:
    seen: set[str] = set()
    _member_name(tarfile.TarInfo("LibriSpeech/LICENSE.TXT"), seen)
    for name in ("LibriSpeech/LICENSE.TXT", "LibriSpeech/license.txt"):
        with pytest.raises(DatasetValidationError, match="case-colliding"):
            _member_name(tarfile.TarInfo(name), seen)


def test_flac_validates_metadata_decode_and_pcm_checksum() -> None:
    content = make_flac(800)
    assert flac_metadata(content, decode=True) == 800
    for invalid in (
        b"not flac",
        content[:42],
        make_flac(sample_rate=8000),
        make_flac(channels=2),
        content[:26] + bytes(16) + content[42:],
    ):
        with pytest.raises(DatasetValidationError):
            flac_metadata(invalid, decode=True)


def test_unconfigured_dataset_is_not_an_empty_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dataset.ENV_REVISION, raising=False)
    external = SpeechDataset()
    assert external.availability().status == "not-configured"
    assert external.availability().sample_count == 26
    with pytest.raises(DatasetUnavailableError, match="Publish"):
        external.samples()


def test_missing_configured_revision_is_invalid(tmp_path: Path) -> None:
    assert SpeechDataset(tmp_path / "missing").availability().status == "invalid"


def test_checksum_failure_publishes_nothing(tmp_path: Path) -> None:
    archive = tmp_path / "invented.tar.gz"
    archive.write_bytes(b"not the official archive")
    with pytest.raises(DatasetValidationError, match="checksum"):
        publish_archive(archive, tmp_path / "revisions")
    assert not (tmp_path / "revisions").exists()


def test_archive_count_and_pairing_are_exact(tmp_path: Path) -> None:
    archive = make_archive(tmp_path / "small.tar.gz", count=2)
    with pytest.raises(DatasetValidationError, match="exactly 2620 paired"):
        _index_archive(archive, "f" * 64)


@pytest.mark.parametrize(
    ("limit", "value", "message"),
    [
        ("MAX_MEMBERS", 1, "count"),
        ("MAX_MEMBER_BYTES", 1, "size"),
        ("MAX_EXPANDED_BYTES", 1, "expansion"),
        ("MAX_EXPANSION_RATIO", 0, "expansion"),
    ],
)
def test_archive_expansion_limits(
    limit: str,
    value: int,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_archive(tmp_path / "bounded.tar.gz", count=2)
    monkeypatch.setattr(dataset, limit, value)
    with pytest.raises(DatasetValidationError, match=message):
        _index_archive(archive, "f" * 64)


def test_duplicate_references_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        add_tar_bytes(
            archive,
            "LibriSpeech/test-clean/1/1/1-1.trans.txt",
            b"1-1-0001 INVENTED\n1-1-0001 INVENTED\n",
        )
    with pytest.raises(DatasetValidationError, match="duplicate transcript"):
        _index_archive(path, "f" * 64)


def test_publication_is_atomic_reproducible_and_detects_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_archive(tmp_path / "synthetic.tar.gz")
    digest = hashlib.md5(archive.read_bytes(), usedforsecurity=False).hexdigest()
    monkeypatch.setattr(dataset, "ARCHIVE_MD5", digest)
    root = tmp_path / "revisions"
    revision = publish_archive(archive, root)
    external = SpeechDataset(revision)
    assert external.availability().status == "ready"
    samples = external.samples()
    assert len(samples) == 26
    assert external.revision.content_hash == sha256_digest(samples)
    assert all(external.read_artifact(sample.input).startswith(b"fLaC") for sample in samples)
    assert len(list(root.iterdir())) == 1
    assert publish_archive(archive, root) == revision
    assert len(list(root.iterdir())) == 1
    snapshot = PublishedSamples.model_validate_json((revision / "samples.json").read_bytes())
    assert snapshot.samples == samples
    selection = json.loads((revision / "selection.json").read_bytes())
    assert selection["coverage"]["utterances"] == 26
    assert selection["anchor_ranks"] == [0, 1309, 2619]
    closure = external.publication_files()
    assert {"publication.json", "inventory.json", "selection.json", "samples.json"} <= set(closure)
    expected_objects = {
        artifact.id
        for sample in samples
        for artifact in (sample.input, sample.reference)
        if artifact is not None
    }
    assert len(closure) == 4 + len(expected_objects)
    assert set(closure) == {
        "publication.json",
        "inventory.json",
        "selection.json",
        "samples.json",
        *(f"artifacts/objects/{identifier[:2]}/{identifier}" for identifier in expected_objects),
    }
    for name, source_path in closure.items():
        assert source_path.read_bytes() == revision.joinpath(*name.split("/")).read_bytes()
    selected = samples[0].input
    content_path = revision / "artifacts" / "objects" / selected.id[:2] / selected.id
    content_path.write_bytes(b"corrupt")
    assert external.availability().status == "invalid"
    with pytest.raises(DatasetValidationError):
        external.publication_files()


def test_incomplete_archive_failure_leaves_no_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_archive(tmp_path / "incomplete.tar.gz", count=2)
    monkeypatch.setattr(
        dataset, "ARCHIVE_MD5", hashlib.md5(archive.read_bytes(), usedforsecurity=False).hexdigest()
    )
    root = tmp_path / "revisions"
    with pytest.raises(DatasetValidationError, match="exactly 2620"):
        publish_archive(archive, root)
    assert list(root.iterdir()) == []


def test_acquisition_is_explicit_bounded_and_checksum_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: io.BytesIO(b"bad"))
    with pytest.raises(DatasetValidationError, match="checksum"):
        dataset.acquire_archive(tmp_path / "archive.tar.gz")
    assert list(tmp_path.iterdir()) == []


def test_repository_is_never_a_dataset_destination() -> None:
    with pytest.raises(DatasetValidationError, match="outside"):
        dataset.require_external(Path.cwd() / "external-data")
