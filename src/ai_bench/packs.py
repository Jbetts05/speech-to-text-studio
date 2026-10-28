"""Benchmark pack discovery and validation."""

from __future__ import annotations

import importlib.util
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast, runtime_checkable

from pydantic import ValidationError

from ai_bench.contracts import (
    ArtifactViewer,
    BenchmarkDefinition,
    Compatibility,
    DatasetAdapter,
    DatasetAvailability,
    DatasetRevision,
    Evaluator,
    ExternalDataset,
    MetricDefinition,
    PackConfiguration,
    PricingModel,
    RendererDescriptor,
    SampleRole,
    SchedulingPolicy,
    SolutionAdapter,
)
from ai_bench.hashing import sha256_digest

PACK_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_discovery_lock = threading.RLock()


class PackValidationError(ValueError):
    pass


class PackNotFoundError(LookupError):
    pass


class DatasetUnavailableError(ValueError):
    def __init__(self, availability: DatasetAvailability) -> None:
        self.availability = availability
        super().__init__(f"dataset {availability.status}: {availability.reason}")


@runtime_checkable
class BenchmarkPack(Protocol):
    @property
    def definition(self) -> BenchmarkDefinition: ...

    @property
    def dataset(self) -> DatasetAdapter: ...

    @property
    def solutions(self) -> tuple[SolutionAdapter, ...]: ...

    @property
    def evaluator(self) -> Evaluator: ...

    @property
    def viewer(self) -> ArtifactViewer: ...


@dataclass(frozen=True)
class DiscoveredPack:
    path: Path
    config: PackConfiguration
    pack: BenchmarkPack


def default_pack_root() -> Path:
    working = Path.cwd() / "benchmark_packs"
    if working.is_dir():
        return working
    source_checkout = Path(__file__).resolve().parents[2] / "benchmark_packs"
    if source_checkout.is_dir():
        return source_checkout
    return Path(__file__).resolve().parents[1] / "benchmark_packs"


def discover_packs(root: Path | None = None) -> dict[str, DiscoveredPack]:
    # Pack loading replaces sys.modules entries; API discovery and preparation
    # must not observe another thread's partly initialized package.
    with _discovery_lock:
        return _discover_packs(root)


def _discover_packs(root: Path | None = None) -> dict[str, DiscoveredPack]:
    pack_root = (root or default_pack_root()).resolve()
    if not pack_root.is_dir():
        return {}
    discovered: dict[str, DiscoveredPack] = {}
    for candidate in sorted(pack_root.iterdir()):
        module_path = candidate / "pack.py"
        if not candidate.is_dir() or candidate.name.startswith("_") or not module_path.is_file():
            continue
        config = _load_config(candidate / "config.json")
        pack = _load_pack(module_path, candidate.name)
        if config.benchmark_id != pack.definition.id:
            raise PackValidationError(
                f"config benchmark_id {config.benchmark_id!r} does not match "
                f"definition id {pack.definition.id!r}"
            )
        if candidate.name != config.benchmark_id:
            raise PackValidationError(
                f"pack directory {candidate.name!r} does not match configured "
                f"benchmark_id {config.benchmark_id!r}"
            )
        validate_pack(pack, config)
        if pack.definition.id in discovered:
            raise PackValidationError(f"duplicate benchmark id: {pack.definition.id}")
        discovered[pack.definition.id] = DiscoveredPack(path=candidate, config=config, pack=pack)
    return discovered


def get_pack(pack_id: str, root: Path | None = None) -> BenchmarkPack:
    if not PACK_ID.fullmatch(pack_id):
        raise PackNotFoundError(pack_id)
    discovered = discover_packs(root)
    if pack_id not in discovered:
        raise PackNotFoundError(pack_id)
    return discovered[pack_id].pack


def validate_pack(pack: object, config: PackConfiguration | None = None) -> None:
    if not isinstance(pack, BenchmarkPack):
        raise PackValidationError(
            "pack must expose definition, dataset, solutions, evaluator, and viewer"
        )
    scheduling_policy(pack)
    if not isinstance(pack.dataset, DatasetAdapter):
        raise PackValidationError(
            "dataset adapter must expose revision, samples(), and read_artifact()"
        )
    if not isinstance(pack.dataset, ExternalDataset):
        validate_dataset(pack)
    if not pack.solutions:
        raise PackValidationError("pack must expose at least one solution")
    if len({solution.id for solution in pack.solutions}) != len(pack.solutions):
        raise PackValidationError("solution identifiers must be unique")
    if config is not None:
        has_networked = any(solution.networked for solution in pack.solutions)
        has_offline = any(not solution.networked for solution in pack.solutions)
        if config.networked_execution != has_networked:
            raise PackValidationError(
                "config networked_execution must match the pack's solution adapters"
            )
        if config.offline_default and not has_offline:
            raise PackValidationError("offline_default requires an offline solution adapter")
    for solution in pack.solutions:
        if not getattr(solution, "version", ""):
            raise PackValidationError(f"solution {solution.id} must declare a version")
        if not isinstance(solution.compatibility, Compatibility):
            raise PackValidationError(f"solution {solution.id} must declare typed compatibility")
        if not isinstance(solution.pricing, PricingModel):
            raise PackValidationError(
                f"solution {solution.id} must declare a typed pricing snapshot"
            )
        if solution.pipeline.adapter_id != solution.id:
            raise PackValidationError(f"solution {solution.id} pipeline adapter_id does not match")
    if not getattr(pack.evaluator, "id", "") or not getattr(pack.evaluator, "version", ""):
        raise PackValidationError("evaluator must declare an id and version")
    if not all(
        isinstance(metric, MetricDefinition) for metric in pack.evaluator.metric_definitions
    ):
        raise PackValidationError("evaluator metrics must be typed metric definitions")
    metric_ids = [metric.id for metric in pack.evaluator.metric_definitions]
    if len(metric_ids) != len(set(metric_ids)):
        raise PackValidationError("evaluator metric identifiers must be unique")
    if not callable(getattr(pack.evaluator, "aggregate", None)):
        raise PackValidationError("evaluator must implement declared metric aggregation")
    if not isinstance(pack.viewer.descriptor, RendererDescriptor):
        raise PackValidationError("viewer must declare a typed renderer descriptor")
    if not pack.viewer.descriptor.version:
        raise PackValidationError("viewer must declare a version")


def scheduling_policy(pack: BenchmarkPack) -> SchedulingPolicy | None:
    policy = getattr(pack, "scheduling_policy", None)
    if policy is not None and not isinstance(policy, SchedulingPolicy):
        raise PackValidationError("scheduling_policy must be a typed versioned policy")
    return policy


def dataset_availability(pack: BenchmarkPack) -> DatasetAvailability:
    if isinstance(pack.dataset, ExternalDataset):
        return pack.dataset.availability()
    samples = tuple(pack.dataset.samples())
    return DatasetAvailability(
        status="ready",
        reason="Bundled dataset is available.",
        revision=pack.dataset.revision,
        sample_count=len(samples),
        scored_sample_count=sum(sample.role is SampleRole.SCORED for sample in samples),
    )


def validate_dataset(pack: BenchmarkPack) -> None:
    if isinstance(pack.dataset, ExternalDataset):
        availability = pack.dataset.availability()
        if availability.status != "ready":
            raise DatasetUnavailableError(availability)
    samples = tuple(pack.dataset.samples())
    if not samples:
        raise PackValidationError("pack dataset must contain samples")
    if len({sample.id for sample in samples}) != len(samples):
        raise PackValidationError("sample identifiers must be unique")
    if not any(sample.role is SampleRole.SCORED for sample in samples):
        raise PackValidationError("pack dataset must contain at least one scored sample")
    if not isinstance(pack.dataset.revision, DatasetRevision):
        raise PackValidationError("dataset adapter must declare a typed revision")
    if pack.dataset.revision.content_hash != sha256_digest(samples):
        raise PackValidationError("dataset revision content hash does not match its samples")
    for sample in samples:
        if sample.input.modality not in pack.definition.input_modalities:
            raise PackValidationError(f"sample {sample.id} has an undeclared input modality")
        if (
            sample.reference
            and sample.reference.modality not in pack.definition.reference_modalities
        ):
            raise PackValidationError(f"sample {sample.id} has an undeclared reference modality")


def _load_config(path: Path) -> PackConfiguration:
    if not path.is_file():
        raise PackValidationError(f"pack is missing required config file: {path}")
    try:
        return PackConfiguration.model_validate_json(path.read_bytes())
    except ValidationError as error:
        raise PackValidationError(f"invalid pack config {path}: {error}") from error


def _load_pack(path: Path, name: str) -> BenchmarkPack:
    package_name = f"_ai_bench_pack_{re.sub(r'[^a-zA-Z0-9_]', '_', name)}_{abs(hash(path))}"
    package = ModuleType(package_name)
    package.__path__ = [str(path.parent)]
    package.__package__ = package_name
    sys.modules[package_name] = package
    module_name = f"{package_name}.pack"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PackValidationError(f"cannot load pack module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return _pack_from_module(module, path)


def _pack_from_module(module: ModuleType, path: Path) -> BenchmarkPack:
    factory = getattr(module, "get_pack", None)
    if callable(factory):
        return cast(BenchmarkPack, factory())
    pack = getattr(module, "PACK", None)
    if pack is None:
        raise PackValidationError(f"{path} must export PACK or get_pack()")
    return cast(BenchmarkPack, pack)
