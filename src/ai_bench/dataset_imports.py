"""Inert, optional pack extension for validating trusted upload staging files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from ai_bench.contracts import BenchmarkSample, DatasetRevision, JsonValue


@dataclass(frozen=True)
class DatasetImportFile:
    name: str
    path: Path
    size_bytes: int
    sha256: str
    media_type: str | None = None


@dataclass(frozen=True)
class DatasetImportResult:
    revision: DatasetRevision
    samples: tuple[BenchmarkSample, ...]
    metadata: Mapping[str, JsonValue]
    files: Mapping[str, Path]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(deepcopy(dict(self.metadata))))
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


class DatasetImporter(Protocol):
    @property
    def descriptor(self) -> Mapping[str, JsonValue]: ...

    def import_files(self, files: tuple[DatasetImportFile, ...]) -> DatasetImportResult: ...


class DatasetAdmissionFile(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def sha256(self) -> str: ...

    @property
    def media_type(self) -> str | None: ...


@runtime_checkable
class DatasetAdmissionValidator(Protocol):
    def validate_admission(self, files: Sequence[DatasetAdmissionFile]) -> None: ...
