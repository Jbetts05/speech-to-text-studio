"""Bounded validation of original uploaded media; never transforms the stored input."""

from __future__ import annotations

import hashlib
import io
import struct
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PureWindowsPath

from .dataset import DatasetValidationError
from .evaluation import normalize_english

MAX_PAIRS = 10
MAX_FILES = 20
MAX_AUDIO_BYTES = 25_000_000
MAX_REFERENCE_BYTES = 16_384
MAX_TOTAL_BYTES = 250_000_000
MAX_AUDIO_SECONDS = 300
MAX_DATASET_SECONDS = 1800
MAX_PCM_BYTES = 128 * 1024 * 1024
MAX_DATASET_PCM_BYTES = 768 * 1024 * 1024
DECODE_CHUNK_FRAMES = 8192
MEDIA_TYPES = {".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
_MIME_ALIASES = {
    ".flac": {"audio/flac", "audio/x-flac"},
    ".wav": {"audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"},
    ".mp3": {"audio/mpeg", "audio/mp3"},
    ".txt": {"text/plain"},
}
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"
}


def validate_filename(name: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", name)
    if (
        not name
        or len(name.encode("utf-8", errors="replace")) > 255
        or name != name.strip()
        or name.endswith(".")
        or normalized != normalized.strip()
        or normalized.endswith(".")
        or any(character in '<>:"/\\|?*' for character in normalized)
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in name
        )
        or PureWindowsPath(normalized).is_absolute()
        or normalized in {".", ".."}
        or normalized.split(".")[0].upper() in _RESERVED
    ):
        raise DatasetValidationError("Upload filenames must be safe single basenames.")
    stem, dot, suffix = name.rpartition(".")
    extension = f".{suffix}"
    if not dot or not stem or stem.endswith((" ", ".")) or extension not in _MIME_ALIASES:
        raise DatasetValidationError("Use exact basename .flac, .wav, .mp3 and .txt pairs.")
    return stem, extension


def validate_mime(extension: str, declared: str | None) -> None:
    if declared is None or declared in {"", "application/octet-stream"}:
        return
    pieces = declared.split(";")
    if pieces[0].strip().lower() not in _MIME_ALIASES[extension]:
        raise DatasetValidationError("Declared MIME does not match the file type.")
    if len(pieces) > 1 and not (
        extension == ".txt"
        and len(pieces) == 2
        and pieces[1].strip().lower() in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise DatasetValidationError("Unexpected media type parameters.")


def validate_reference(content: bytes) -> str:
    if not 0 < len(content) <= MAX_REFERENCE_BYTES:
        raise DatasetValidationError("Reference exceeds the UTF-8 byte limit or is empty.")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DatasetValidationError("Reference must be strict UTF-8.") from error
    if any(
        unicodedata.category(character).startswith("C") and character not in "\t\r\n"
        for character in text
    ):
        raise DatasetValidationError("Reference contains unsafe control characters.")
    if not normalize_english(text):
        raise DatasetValidationError("Reference must contain normalized English-policy tokens.")
    return text


@dataclass(frozen=True)
class ValidatedAudio:
    media_type: str
    extension: str
    frames: int
    sample_rate: int
    channels: int
    decoded_pcm_bytes: int

    @property
    def duration_seconds(self) -> Decimal:
        return Decimal(self.frames) / self.sample_rate


def _flac_info(content: bytes) -> tuple[int, int, int, int, bytes]:
    if len(content) < 42 or content[:4] != b"fLaC":
        raise DatasetValidationError("Invalid FLAC signature.")
    position, index = 4, 0
    result: tuple[int, int, int, int, bytes] | None = None
    while True:
        if index >= 128:
            raise DatasetValidationError("FLAC metadata block count exceeds its resource bound.")
        if position + 4 > len(content):
            raise DatasetValidationError("Truncated FLAC metadata.")
        header = content[position]
        kind = header & 127
        size = int.from_bytes(content[position + 1 : position + 4], "big")
        position += 4
        if position + size > len(content) or kind == 127:
            raise DatasetValidationError("Invalid FLAC metadata length or type.")
        block = content[position : position + size]
        if index == 0:
            if kind != 0 or size != 34:
                raise DatasetValidationError("FLAC requires leading STREAMINFO.")
            packed = int.from_bytes(block[10:18], "big")
            result = (
                packed & ((1 << 36) - 1),
                packed >> 44,
                ((packed >> 41) & 7) + 1,
                ((packed >> 36) & 31) + 1,
                block[18:34],
            )
            if result[3] not in {8, 16, 24, 32} or result[4] == bytes(16):
                raise DatasetValidationError("FLAC requires supported PCM and a PCM checksum.")
        elif kind == 0:
            raise DatasetValidationError("Duplicate FLAC STREAMINFO.")
        position += size
        index += 1
        if header & 128:
            break
    if (
        result is None
        or position + 2 > len(content)
        or content[position] != 255
        or content[position + 1] & 252 != 248
    ):
        raise DatasetValidationError("FLAC contains no audio frames.")
    return result


def _wav_info(content: bytes) -> tuple[int, int, int]:
    if (
        len(content) < 44
        or content[:4] != b"RIFF"
        or content[8:12] != b"WAVE"
        or int.from_bytes(content[4:8], "little") + 8 != len(content)
    ):
        raise DatasetValidationError("Invalid or truncated RIFF/WAVE container.")
    position = 12
    wave_format: tuple[int, int, int, int, int, int] | None = None
    data_size: int | None = None
    chunks = 0
    while position < len(content):
        chunks += 1
        if chunks > 1024:
            raise DatasetValidationError("WAVE chunk count exceeds its resource bound.")
        if position + 8 > len(content):
            raise DatasetValidationError("Truncated WAVE chunk header.")
        kind = content[position : position + 4]
        size = int.from_bytes(content[position + 4 : position + 8], "little")
        position += 8
        if position + size + (size % 2) > len(content):
            raise DatasetValidationError("Truncated WAVE chunk.")
        if kind == b"fmt ":
            if wave_format is not None or size < 16:
                raise DatasetValidationError("Invalid WAVE format chunk.")
            wave_format = struct.unpack("<HHIIHH", content[position : position + 16])
            if wave_format[0] == 0xFFFE:
                if (
                    size < 40
                    or int.from_bytes(content[position + 16 : position + 18], "little") != 22
                    or content[position + 26 : position + 40]
                    != b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
                ):
                    raise DatasetValidationError("Invalid extensible WAVE PCM format.")
                valid_bits = int.from_bytes(content[position + 18 : position + 20], "little")
                if not 0 < valid_bits <= wave_format[5]:
                    raise DatasetValidationError("Invalid extensible WAVE bit depth.")
                wave_format = (
                    int.from_bytes(content[position + 24 : position + 26], "little"),
                    *wave_format[1:],
                )
        elif kind == b"data":
            if data_size is not None or wave_format is None:
                raise DatasetValidationError("WAVE must have one data chunk after its format.")
            data_size = size
        elif kind in {b"LIST", b"RIFF", b"plst"}:
            # WAVE playlists, embedded containers and multi-stream formats are not uploads.
            raise DatasetValidationError("WAVE playlists or nested containers are unsupported.")
        position += size + size % 2
    if wave_format is None or data_size is None:
        raise DatasetValidationError("WAVE format or data is missing.")
    encoding, channels, rate, byte_rate, alignment, bits = wave_format
    if (
        encoding not in {1, 3}
        or bits not in ({8, 16, 24, 32} if encoding == 1 else {32, 64})
        or not alignment
        or alignment != channels * (bits // 8)
        or byte_rate != rate * alignment
        or data_size % alignment
    ):
        raise DatasetValidationError("Unsupported or inconsistent WAVE PCM format.")
    return data_size // alignment, rate, channels


def _mp3_info(content: bytes) -> tuple[int, int, int]:
    position = 0
    if content[:3] == b"ID3":
        if (
            len(content) < 10
            or content[3] not in {2, 3, 4}
            or content[4] == 255
            or any(value & 128 for value in content[6:10])
        ):
            raise DatasetValidationError("Invalid MP3 ID3 header.")
        allowed_flags = {2: 0x80, 3: 0xE0, 4: 0xF0}[content[3]]
        if content[5] & ~allowed_flags:
            raise DatasetValidationError("Unsupported MP3 ID3 header flags.")
        size = sum(
            value << shift for value, shift in zip(content[6:10], (21, 14, 7, 0), strict=True)
        )
        position = 10 + size
        if content[3] == 4 and content[5] & 16:
            if (
                content[position : position + 3] != b"3DI"
                or content[position + 3 : position + 10] != content[3:10]
            ):
                raise DatasetValidationError("Invalid MP3 ID3 footer.")
            position += 10
        if position >= len(content):
            raise DatasetValidationError("Truncated MP3 tag or missing MPEG audio.")
    end = len(content) - 128 if content[-128:-125] == b"TAG" else len(content)
    count = 0
    encoded_samples = 0
    declared_frames: int | None = None
    tag_frame = False
    identity: tuple[int, int] | None = None
    while position < end:
        if position + 4 > end:
            raise DatasetValidationError("Truncated MPEG frame header.")
        header = int.from_bytes(content[position : position + 4], "big")
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_index, rate_index = (header >> 12) & 15, (header >> 10) & 3
        if (
            header >> 21 != 2047
            or version == 1
            or layer != 1
            or bitrate_index in {0, 15}
            or rate_index == 3
            or header & 3 == 2
        ):
            raise DatasetValidationError("Invalid MP3 MPEG Layer III frame.")
        rate = (44100, 48000, 32000)[rate_index] // (
            1 if version == 3 else 2 if version == 2 else 4
        )
        channels = 1 if (header >> 6) & 3 == 3 else 2
        bitrates = (
            (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
            if version == 3
            else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
        )
        frame_bytes = (144 if version == 3 else 72) * bitrates[bitrate_index] * 1000 // rate + (
            (header >> 9) & 1
        )
        if position + frame_bytes > end or (identity is not None and identity != (rate, channels)):
            raise DatasetValidationError("Truncated MP3 frame or changing stream format.")
        frame_samples = 1152 if version == 3 else 576
        if count == 0:
            side_bytes = (
                (17 if channels == 1 else 32) if version == 3 else (9 if channels == 1 else 17)
            )
            marker = position + 4 + (0 if (header >> 16) & 1 else 2) + side_bytes
            if content[marker : marker + 4] in {b"Xing", b"Info"}:
                tag_frame = True
                flags = int.from_bytes(content[marker + 4 : marker + 8], "big")
                if flags & ~15 or not flags & 1 or marker + 12 > position + frame_bytes:
                    raise DatasetValidationError("Invalid MP3 frame-count header.")
                declared_frames = int.from_bytes(content[marker + 8 : marker + 12], "big")
            elif content[position + 36 : position + 40] == b"VBRI":
                marker = position + 36
                if marker + 18 > position + frame_bytes:
                    raise DatasetValidationError("Truncated MP3 VBR frame-count header.")
                declared_frames = int.from_bytes(content[marker + 14 : marker + 18], "big")
        if count or not tag_frame:
            encoded_samples += frame_samples
        identity = rate, channels
        position += frame_bytes
        count += 1
        if count > 60_000:
            raise DatasetValidationError("MP3 frame count exceeds bounded duration.")
    if not count or identity is None:
        raise DatasetValidationError("MP3 contains no MPEG audio.")
    if declared_frames is not None and declared_frames != count - int(tag_frame):
        raise DatasetValidationError("MP3 frame-count header differs from the complete stream.")
    return *identity, encoded_samples


def validate_audio(
    content: bytes, extension: str, declared_mime: str | None = None
) -> ValidatedAudio:
    if extension not in MEDIA_TYPES or not 0 < len(content) <= MAX_AUDIO_BYTES:
        raise DatasetValidationError("Audio type or byte count is outside upload limits.")
    validate_mime(extension, declared_mime)
    flac = _flac_info(content) if extension == ".flac" else None
    wave = _wav_info(content) if extension == ".wav" else None
    mpeg = _mp3_info(content) if extension == ".mp3" else None
    import soundfile

    try:
        with soundfile.SoundFile(io.BytesIO(content)) as audio:
            frames, rate, channels = audio.frames, audio.samplerate, audio.channels
            expected_formats = {".flac": {"FLAC"}, ".wav": {"WAV", "WAVEX"}, ".mp3": {"MP3"}}[
                extension
            ]
            if (
                audio.format not in expected_formats
                or not 8000 <= rate <= 48000
                or channels not in {1, 2}
                or not 0 < frames <= MAX_AUDIO_SECONDS * rate
                or frames * channels * 4 > MAX_PCM_BYTES
                or (flac is not None and flac[:3] != (frames, rate, channels))
                or (wave is not None and wave != (frames, rate, channels))
                or (
                    mpeg is not None
                    and (mpeg[:2] != (rate, channels) or not frames <= mpeg[2] <= frames + 8192)
                )
            ):
                raise DatasetValidationError("Decoded audio metadata is invalid or exceeds limits.")
            decoded_frames = 0
            pcm_hash = hashlib.md5(usedforsecurity=False)
            while True:
                block = audio.read(
                    DECODE_CHUNK_FRAMES,
                    dtype="int32" if flac is not None else "float32",
                    always_2d=True,
                )
                if not len(block):
                    break
                decoded_frames += len(block)
                if (
                    decoded_frames > frames
                    or decoded_frames * channels * 4 > MAX_PCM_BYTES
                    or decoded_frames > MAX_AUDIO_SECONDS * rate
                ):
                    raise DatasetValidationError(
                        "Decoded PCM exceeds its declared resource bounds."
                    )
                if flac is not None:
                    width = flac[3] // 8
                    native = block.astype("<i4", copy=False).view("uint8").reshape(-1, 4)
                    pcm_hash.update(native[:, 4 - width :].tobytes())
                elif not bool(((block <= 1e38) & (block >= -1e38)).all()):
                    raise DatasetValidationError("Decoded PCM contains non-finite sample values.")
            if decoded_frames != frames or (flac is not None and pcm_hash.digest() != flac[4]):
                raise DatasetValidationError("Truncated audio or decoded PCM checksum mismatch.")
            if any(word in audio.extra_info.lower() for word in ("error", "truncat", "corrupt")):
                raise DatasetValidationError("Audio decoder reported damaged media.")
            return ValidatedAudio(
                MEDIA_TYPES[extension], extension, frames, rate, channels, frames * channels * 4
            )
    except (soundfile.LibsndfileError, ValueError, RuntimeError) as error:
        if isinstance(error, DatasetValidationError):
            raise
        raise DatasetValidationError("Audio cannot be completely decoded.") from error
