"""Versioned external publication records, not bundled dataset fixtures."""

from __future__ import annotations

from fractions import Fraction
from typing import Final, Literal

from pydantic import Field, model_validator

from ai_bench.contracts import BenchmarkSample, Contract, DatasetRevision

RELEASE: Final = "librispeech-test-clean"
SOURCE_URL = "https://www.openslr.org/12"
ARCHIVE_URL = "https://www.openslr.org/resources/12/test-clean.tar.gz"
CHECKSUM_URL = "https://www.openslr.org/resources/12/md5sum.txt"
ARCHIVE_MD5 = "32fa31d27d2e1cad72775fee3f4849a9"
UTTERANCES = 2620
PILOT_SIZE = 26
SELECTOR_VERSION: Final = "duration-strata-speaker-chapter-v1"
SELECTOR_SEED: Final = "speech-to-text-pilot-v1"
ATTRIBUTION = (
    "Vassil Panayotov, Guoguo Chen, Daniel Povey, and Sanjeev Khudanpur. "
    "LibriSpeech: an ASR corpus based on public domain audio books. ICASSP 2015. "
    "OpenSLR SLR12; CC BY 4.0. Original FLAC and references are unmodified."
)


class InventoryEntry(Contract):
    id: str = Field(pattern=r"^[0-9]+-[0-9]+-[0-9]+$")
    speaker: str = Field(pattern=r"^[0-9]+$")
    chapter: str = Field(pattern=r"^[0-9]+$")
    frames: int = Field(gt=0, le=960000)
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    bits_per_sample: Literal[16] = 16
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_bytes: int = Field(gt=0, le=16777216)
    reference_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reference_bytes: int = Field(gt=0, le=16384)

    @model_validator(mode="after")
    def identity_matches_components(self) -> InventoryEntry:
        if self.id.split("-")[:2] != [self.speaker, self.chapter]:
            raise ValueError("utterance identity does not match speaker/chapter")
        return self

    @property
    def duration(self) -> Fraction:
        return Fraction(self.frames, self.sample_rate)


class Inventory(Contract):
    schema_version: Literal["1"] = "1"
    release: Literal["librispeech-test-clean"] = RELEASE
    source: str = SOURCE_URL
    license: Literal["CC BY 4.0"] = "CC BY 4.0"
    attribution: str = ATTRIBUTION
    archive_md5: str = Field(pattern=r"^[a-f0-9]{32}$")
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    entries: tuple[InventoryEntry, ...] = Field(min_length=UTTERANCES, max_length=UTTERANCES)

    @model_validator(mode="after")
    def unique_ordered_entries(self) -> Inventory:
        identifiers = tuple(entry.id for entry in self.entries)
        if len(set(identifiers)) != UTTERANCES or identifiers != tuple(sorted(identifiers)):
            raise ValueError("inventory must contain 2620 unique ID-ordered utterances")
        return self


class SelectedUtterance(Contract):
    id: str
    rank: int = Field(ge=0, lt=UTTERANCES)
    stratum: int = Field(ge=0, lt=PILOT_SIZE)
    input_hash: str
    reference_hash: str


class SelectionCoverage(Contract):
    utterances: Literal[26] = 26
    strata: Literal[26] = 26
    speakers: int = Field(ge=1, le=26)
    chapters: int = Field(ge=1, le=26)
    total_frames: int = Field(gt=0)
    sample_rate: Literal[16000] = 16000
    minimum_frames: int = Field(gt=0)
    maximum_frames: int = Field(gt=0)


class PilotSelection(Contract):
    schema_version: Literal["1"] = "1"
    selector_version: Literal["duration-strata-speaker-chapter-v1"] = SELECTOR_VERSION
    seed: Literal["speech-to-text-pilot-v1"] = SELECTOR_SEED
    release: Literal["librispeech-test-clean"] = RELEASE
    inventory_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    anchor_ranks: tuple[int, ...] = (0, 1309, 2619)
    selected: tuple[SelectedUtterance, ...] = Field(min_length=26, max_length=26)
    coverage: SelectionCoverage


class Publication(Contract):
    schema_version: Literal["1"] = "1"
    revision: DatasetRevision
    inventory_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    selection_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    samples_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class PublishedSamples(Contract):
    samples: tuple[BenchmarkSample, ...] = Field(min_length=26, max_length=26)
