from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_bench.artifacts import FileArtifactStore
from ai_bench.contracts import Modality


def test_concurrent_content_addressed_writes_are_atomic(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    content = b"shared synthetic evidence"

    def write() -> str:
        return store.put_bytes(
            content,
            modality=Modality.TEXT,
            media_type="text/plain",
        ).id

    with ThreadPoolExecutor(max_workers=8) as executor:
        artifact_ids = list(executor.map(lambda _: write(), range(32)))

    assert len(set(artifact_ids)) == 1
    assert store.get_bytes(artifact_ids[0]) == content
    assert not tuple(store.objects.rglob("*.tmp"))


def test_identical_bytes_retain_media_type_variants_deterministically(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    content = b'{"intent":"support"}'
    text = store.put_bytes(
        content,
        modality=Modality.TEXT,
        media_type="text/plain",
    )
    structured = store.put_bytes(
        content,
        modality=Modality.STRUCTURED,
        media_type="application/json",
    )

    assert text.id == structured.id
    assert store.get(text.id).media_type == "application/json"
    assert store.get(text.id, "text/plain").modality is Modality.TEXT
    assert store.get(text.id, "application/json").modality is Modality.STRUCTURED
