from __future__ import annotations

import io
import os
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import soundfile

from ai_bench.api_contracts import DatasetImportDescription
from ai_bench.dataset_imports import DatasetImporter, DatasetImportFile
from ai_bench.hashing import canonical_json, content_digest, sha256_digest
from benchmark_packs.speech_to_text import upload, upload_media
from benchmark_packs.speech_to_text.dataset import DatasetValidationError, flac_metadata
from benchmark_packs.speech_to_text.decode_process import decoder_environment
from benchmark_packs.speech_to_text.pack import get_pack
from benchmark_packs.speech_to_text.upload import SpeechDatasetImporter
from benchmark_packs.speech_to_text.upload_media import (
    validate_audio,
    validate_filename,
    validate_reference,
)
from tests.speech_support import make_audio


def stage_file(
    root: Path, name: str, content: bytes, media_type: str | None = None
) -> DatasetImportFile:
    path = root / f"part-{len(tuple(root.iterdir()))}"
    path.write_bytes(content)
    return DatasetImportFile(name, path, len(content), content_digest(content), media_type)


def stage_pair(
    root: Path, extension: str = ".flac", stem: str = "invented"
) -> tuple[DatasetImportFile, DatasetImportFile]:
    return (
        stage_file(root, f"{stem}{extension}", make_audio(extension)),
        stage_file(root, f"{stem}.txt", b"Synthetic reference.\n", "text/plain; charset=utf-8"),
    )


@pytest.mark.parametrize("extension", [".flac", ".wav", ".mp3"])
@pytest.mark.parametrize(("rate", "channels"), [(8000, 1), (24000, 2), (48000, 2)])
def test_upload_decodes_original_media_and_preserves_every_byte(
    tmp_path: Path, extension: str, rate: int, channels: int
) -> None:
    content = make_audio(extension, frames=4800, sample_rate=rate, channels=channels)
    inputs = (
        stage_file(tmp_path, f"invented{extension}", content),
        stage_file(tmp_path, "invented.txt", b"Keep THIS original.\r\n"),
    )
    result = SpeechDatasetImporter().import_files(inputs)
    assert result == SpeechDatasetImporter().import_files(tuple(reversed(inputs)))
    assert result.revision.content_hash == sha256_digest(result.samples)
    assert len(result.samples) == 1
    sample = result.samples[0]
    assert sample.input.metadata["sample_rate"] == rate
    assert sample.input.metadata["channels"] == channels
    assert sample.input.metadata["frames"] == 4800
    assert Decimal(str(sample.input.metadata["duration_seconds"])) == Decimal(4800) / rate
    assert result.files[sample.input.id].read_bytes() == content
    assert sample.reference is not None
    assert result.files[sample.reference.id].read_bytes() == b"Keep THIS original.\r\n"
    assert sample.reference.metadata["text"] == "Keep THIS original.\r\n"
    assert sample.metadata["language"] == "en"
    assert result.metadata["original_bytes_preserved"] is True
    assert all(name not in str(result.samples) for name in ("invented.txt", f"invented{extension}"))
    with pytest.raises(TypeError):
        result.files["a" * 64] = inputs[0].path  # type: ignore[index]


def test_descriptor_is_inert_versioned_and_api_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("discovery cannot touch files, decode or obtain credentials")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(soundfile, "SoundFile", forbidden)
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", forbidden)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", forbidden)
    pack = get_pack()
    description = DatasetImportDescription.model_validate_json(
        canonical_json(pack.dataset_importer.descriptor)
    )
    assert description.max_files == 20
    assert description.max_input_bytes == 25_000_000
    assert description.max_reference_bytes == 16_384
    assert description.max_total_bytes == 250_000_000
    assert description.language == "en"
    assert pack.definition.version == "1.1.0"
    assert pack.viewer.descriptor.version == "1.1.0"
    assert set(upload_media.MEDIA_TYPES.values()) <= set(pack.viewer.descriptor.media_types)


@pytest.mark.parametrize(
    "name",
    [
        "../word.wav",
        "/word.wav",
        r"C:\word.wav",
        r"a\b.wav",
        "a/b.wav",
        "CON.wav",
        "con.txt",
        "COM1.mp3",
        "LPT¹.wav",
        "NUL.foo.wav",
        " word.wav",
        "word.wav ",
        "word..wav",
        "word.wav.",
        "word.wav:stream",
        "word\x00.wav",
        "word\n.wav",
        "word\u202e.wav",
        "word\u2066.wav",
        "word\u2028.wav",
        "word\u200b.wav",
        "word\uff0fpart.wav",
        "word\uff1apart.wav",
        "file.zip",
        "file.m3u",
        "file.WAV",
        "https://example.invalid/word.wav",
        "x" * 260 + ".wav",
    ],
)
def test_unsafe_or_unsupported_names_are_rejected(name: str) -> None:
    with pytest.raises(DatasetValidationError):
        validate_filename(name)


@pytest.mark.parametrize(
    "names",
    [
        ("name.wav", "NAME.txt"),
        ("Café.wav", "Cafe\u0301.txt"),
        ("\uff21.wav", "A.txt"),
        ("name.wav", "name.wav"),
        ("name.wav", "other.txt"),
        ("name.wav", "name.mp3"),
    ],
)
def test_no_guessing_duplicate_or_colliding_pair_names(
    tmp_path: Path, names: tuple[str, str]
) -> None:
    files = stage_pair(tmp_path, ".wav")
    with pytest.raises(DatasetValidationError):
        SpeechDatasetImporter().import_files(
            (replace(files[0], name=names[0]), replace(files[1], name=names[1]))
        )


@pytest.mark.parametrize(
    "text",
    [b"", b" . , ! -- ", b"\x00word", b"\xff", b"a\x01", "a\u202e".encode(), "a\u200b".encode()],
)
def test_empty_normalized_or_unsafe_utf8_reference_is_rejected(text: bytes) -> None:
    with pytest.raises(DatasetValidationError):
        validate_reference(text)


def test_reference_byte_bound_is_utf8_not_character_count() -> None:
    assert validate_reference(b"a" * 16_384) == "a" * 16_384
    with pytest.raises(DatasetValidationError):
        validate_reference(("é" * 8193).encode())


@pytest.mark.parametrize("count", [0, 1, 3, 22])
def test_file_count_admission_is_bounded(tmp_path: Path, count: int) -> None:
    audio, _ = stage_pair(tmp_path)
    with pytest.raises(DatasetValidationError):
        SpeechDatasetImporter().import_files(tuple(audio for _ in range(count)))


def test_exactly_twenty_files_ten_pairs_are_accepted(tmp_path: Path) -> None:
    files = tuple(file for index in range(10) for file in stage_pair(tmp_path, stem=f"s{index}"))
    result = SpeechDatasetImporter().import_files(files)
    assert len(result.samples) == 10
    assert len({sample.id for sample in result.samples}) == 10


@pytest.mark.parametrize(("index", "size"), [(0, 25_000_001), (1, 16_385), (0, 0)])
def test_per_role_declared_byte_limits(tmp_path: Path, index: int, size: int) -> None:
    files = list(stage_pair(tmp_path))
    files[index] = replace(files[index], size_bytes=size)
    with pytest.raises(DatasetValidationError):
        SpeechDatasetImporter().import_files(tuple(files))


def test_aggregate_byte_bound_before_any_file_read(tmp_path: Path) -> None:
    files = tuple(
        replace(file, size_bytes=25_000_000 if position == 0 else 16_384)
        for index in range(10)
        for position, file in enumerate(stage_pair(tmp_path, stem=f"s{index}"))
    )
    with pytest.raises(DatasetValidationError, match="aggregate"):
        SpeechDatasetImporter().import_files(files)


@pytest.mark.parametrize("change", ["hash", "size", "hardlink", "directory"])
def test_staged_identity_and_regular_unlinked_files_required(tmp_path: Path, change: str) -> None:
    audio, reference = stage_pair(tmp_path)
    if change == "hash":
        audio = replace(audio, sha256="0" * 64)
    elif change == "size":
        audio.path.write_bytes(audio.path.read_bytes() + b"x")
    elif change == "hardlink":
        os.link(audio.path, tmp_path / "linked")
    else:
        audio = replace(audio, path=tmp_path)
    with pytest.raises(DatasetValidationError):
        SpeechDatasetImporter().import_files((audio, reference))


@pytest.mark.parametrize("extension", [".flac", ".wav", ".mp3"])
def test_signature_truncation_wrong_container_and_mime_rejected(extension: str) -> None:
    content = make_audio(extension)
    for invalid in (b"not audio", content[:20], content[:-1]):
        with pytest.raises(DatasetValidationError):
            validate_audio(invalid, extension)
    with pytest.raises(DatasetValidationError, match="MIME"):
        validate_audio(content, extension, "text/html")
    with pytest.raises(DatasetValidationError):
        validate_audio(make_audio(".wav" if extension != ".wav" else ".flac"), extension)


@pytest.mark.parametrize(("rate", "channels"), [(7999, 1), (48001, 1), (16000, 3)])
def test_decoded_metadata_limits(rate: int, channels: int) -> None:
    with pytest.raises(DatasetValidationError):
        validate_audio(make_audio(".wav", sample_rate=rate, channels=channels), ".wav")


def test_original_librispeech_validation_is_not_weakened() -> None:
    content = make_audio(".flac", sample_rate=24000, channels=2)
    assert validate_audio(content, ".flac").channels == 2
    with pytest.raises(DatasetValidationError, match="16 kHz mono"):
        flac_metadata(content, decode=True)


def test_full_decode_is_chunked_and_pcm_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    content = make_audio(".wav", frames=20000)
    original = soundfile.SoundFile.read
    reads: list[int] = []

    def checked_read(self: Any, frames: int, **kwargs: Any) -> Any:
        reads.append(frames)
        assert frames <= 8192
        return original(self, frames, **kwargs)

    monkeypatch.setattr(soundfile.SoundFile, "read", checked_read)
    assert validate_audio(content, ".wav").frames == 20000
    assert len(reads) == 4
    monkeypatch.setattr(upload_media, "MAX_PCM_BYTES", 20000 * 4 - 1)
    with pytest.raises(DatasetValidationError, match="metadata"):
        validate_audio(content, ".wav")


def test_aggregate_duration_and_pcm_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = (*stage_pair(tmp_path, stem="first"), *stage_pair(tmp_path, stem="second"))
    monkeypatch.setattr(upload, "MAX_DATASET_SECONDS", Decimal("0.5"))
    with pytest.raises(DatasetValidationError, match="Dataset duration"):
        SpeechDatasetImporter()._import_files(files)
    monkeypatch.setattr(upload, "MAX_DATASET_SECONDS", 1800)
    monkeypatch.setattr(upload, "MAX_DATASET_PCM_BYTES", 2 * 4800 * 4 - 1)
    with pytest.raises(DatasetValidationError, match="decoded PCM"):
        SpeechDatasetImporter()._import_files(files)


def test_per_file_duration_limit_and_zero_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_media, "MAX_AUDIO_SECONDS", Decimal("0.3"))
    assert validate_audio(make_audio(".wav", frames=4800), ".wav").frames == 4800
    for frames in (0, 4801):
        with pytest.raises(DatasetValidationError):
            validate_audio(make_audio(".wav", frames=frames), ".wav")


def test_extensible_wave_pcm_is_decoded_without_conversion() -> None:
    stream = io.BytesIO()
    with soundfile.SoundFile(
        stream, mode="w", samplerate=48000, channels=2, format="WAVEX", subtype="PCM_16"
    ) as output:
        output.buffer_write(bytes(4800 * 2 * 2), dtype="int16")
    result = validate_audio(stream.getvalue(), ".wav")
    assert result.frames == 4800
    assert result.sample_rate == 48000 and result.channels == 2


def test_wave_playlist_and_mp3_damaged_tag_are_rejected() -> None:
    wave = make_audio(".wav")
    playlist = wave + b"plst" + bytes(4)
    playlist = playlist[:4] + (len(playlist) - 8).to_bytes(4, "little") + playlist[8:]
    with pytest.raises(DatasetValidationError, match="playlist"):
        validate_audio(playlist, ".wav")
    mp3 = make_audio(".mp3")
    for header in (
        b"ID3\x04\x00\x10" + bytes(4) + b"invalidtag",
        b"ID3\x04\x00\x01" + bytes(4),
        b"ID3\x04\x00\x00\x80\x00\x00\x00",
    ):
        with pytest.raises(DatasetValidationError, match="ID3"):
            validate_audio(header + mp3, ".mp3")


@pytest.mark.parametrize("predicate", ["is_symlink", "is_junction"])
def test_staged_links_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, predicate: str
) -> None:
    audio, reference = stage_pair(tmp_path)
    original = getattr(Path, predicate)
    monkeypatch.setattr(Path, predicate, lambda path: path == audio.path or original(path))
    with pytest.raises(DatasetValidationError, match="links"):
        SpeechDatasetImporter()._import_files((audio, reference))


def test_mp3_cannot_hide_undecoded_frames_with_false_xing_count() -> None:
    content = make_audio(".mp3")
    marker = content.index(b"Xing")
    forged = content[: marker + 8] + (1).to_bytes(4, "big") + content[marker + 12 :]
    with pytest.raises(DatasetValidationError, match="frame-count"):
        validate_audio(forged, ".mp3")


def test_container_metadata_work_is_bounded() -> None:
    flac = make_audio(".flac")
    too_many_blocks = flac[:42] + b"\x01\x00\x00\x00" * 129 + flac[42:]
    with pytest.raises(DatasetValidationError, match="resource bound"):
        validate_audio(too_many_blocks, ".flac")
    wave = make_audio(".wav") + (b"JUNK" + bytes(4)) * 1025
    wave = wave[:4] + (len(wave) - 8).to_bytes(4, "little") + wave[8:]
    with pytest.raises(DatasetValidationError, match="resource bound"):
        validate_audio(wave, ".wav")


def test_discovered_pack_uses_a_real_importable_decoder_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_bench.packs import get_pack as discover

    for name in (
        "AZURE_CLIENT_SECRET",
        "IDENTITY_ENDPOINT",
        "IDENTITY_HEADER",
        "AI_BENCH_SPEECH_ENDPOINT",
        "AZURE_FEDERATED_TOKEN_FILE",
        "HOME",
        "USERPROFILE",
    ):
        monkeypatch.setenv(name, "must-not-reach-decoder")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "128")
    original_run = subprocess.run
    observed: list[subprocess.CompletedProcess[bytes]] = []

    def real_worker(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == [sys.executable, "-m", "benchmark_packs.speech_to_text.decode_worker"]
        assert "must-not-reach-decoder" not in kwargs["env"].values()
        assert kwargs["env"]["OPENBLAS_NUM_THREADS"] == "1"
        completed = original_run(*args, **kwargs)
        observed.append(completed)
        return completed

    monkeypatch.setattr(subprocess, "run", real_worker)
    pack = discover("speech_to_text")
    assert type(pack).__module__.startswith("_ai_bench_pack_")
    importer = cast(DatasetImporter, vars(pack)["dataset_importer"])
    result = importer.import_files(stage_pair(tmp_path, ".mp3"))
    assert len(result.samples) == 1
    assert result.samples[0].input.media_type == "audio/mpeg"
    assert len(observed) == 1 and observed[0].returncode == 0


def test_decoder_environment_does_not_inherit_identity_or_python_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AI_BENCH_AUTH_MODE",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "MSI_ENDPOINT",
        "MSI_SECRET",
        "IDENTITY_ENDPOINT",
        "IDENTITY_HEADER",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    ):
        monkeypatch.setenv(name, "not-inherited")
    environment = decoder_environment()
    assert "not-inherited" not in environment.values()
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        assert environment[variable] == "1"


def test_decoder_timeout_is_a_clear_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = subprocess.run

    def time_out(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        kwargs["timeout"] = 0.001
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", time_out)
    with pytest.raises(DatasetValidationError, match="time limit"):
        SpeechDatasetImporter().import_files(stage_pair(tmp_path))


def test_windows_job_limiter_rejects_other_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    from benchmark_packs.speech_to_text import decode_worker

    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "platform", "linux")
        with pytest.raises(RuntimeError, match="Windows decoder job limits require Windows"):
            decode_worker._windows_limits()


def test_decoder_os_memory_limit_is_enforced_in_a_real_process() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from benchmark_packs.speech_to_text.decode_worker import "
            "apply_resource_limits, MEMORY_LIMIT_BYTES\n"
            "apply_resource_limits()\n"
            "try:\n"
            "    memory = bytearray(MEMORY_LIMIT_BYTES * 2)\n"
            "except MemoryError:\n"
            "    print('bounded')\n"
            "else:\n"
            "    raise SystemExit(1)\n",
        ],
        env=decoder_environment(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout.strip() == b"bounded"
