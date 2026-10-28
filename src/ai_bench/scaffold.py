"""Benchmark brief validation and safe pack scaffolding."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ai_bench.packs import PACK_ID

REQUIRED_BRIEF_SECTIONS = (
    "## Identity",
    "## Decision this benchmark supports",
    "## Modality",
    "## Dataset",
    "## Solutions",
    "## Evaluation",
    "## Compatibility",
    "## Evidence and reports",
    "## Runtime and durability",
    "## Azure deployment",
    "## Acceptance cases",
    "## Out of scope",
)
UNRESOLVED_MARKER = re.compile(r"\{\{[^}]+\}\}|TODO|TBD", re.IGNORECASE)


class BriefValidationError(ValueError):
    pass


def validate_brief(path: Path) -> None:
    if not path.is_file():
        raise BriefValidationError(f"benchmark brief does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    missing = [section for section in REQUIRED_BRIEF_SECTIONS if section not in text]
    if missing:
        raise BriefValidationError(f"missing required sections: {', '.join(missing)}")
    marker = UNRESOLVED_MARKER.search(text)
    if marker:
        raise BriefValidationError(f"unresolved marker in benchmark brief: {marker.group(0)}")


def scaffold_pack(slug: str, root: Path, brief: Path | None = None) -> Path:
    if not PACK_ID.fullmatch(slug):
        raise BriefValidationError(
            "pack slug must be 3-64 lowercase letters, digits, underscores, or hyphens"
        )
    if brief is not None:
        validate_brief(brief)
    destination = root.resolve() / slug
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing pack: {destination}")
    destination.mkdir(parents=True)
    templates = {
        "__init__.py": _INIT_TEMPLATE,
        "dataset.py": _DATASET_TEMPLATE,
        "solutions.py": _SOLUTIONS_TEMPLATE,
        "evaluator.py": _EVALUATOR_TEMPLATE,
        "viewer.py": _VIEWER_TEMPLATE,
        "pack.py": _PACK_TEMPLATE,
        "test_pack.py": _TEST_TEMPLATE,
    }
    for name, template in templates.items():
        (destination / name).write_text(template.replace("__SLUG__", slug), encoding="ascii")
    (destination / "config.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "benchmark_id": slug,
                "offline_default": True,
                "networked_execution": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="ascii",
    )
    return destination


_INIT_TEMPLATE = '''"""Benchmark pack extension."""
'''

_DATASET_TEMPLATE = '''"""Dataset adapter for __SLUG__."""

from collections.abc import Sequence

from ai_bench.contracts import (
    BenchmarkSample,
    DatasetProvenance,
    DatasetRevision,
    ModalityArtifact,
)
from ai_bench.hashing import sha256_digest


class Dataset:
    id = "__SLUG__-dataset-v1"
    revision = DatasetRevision(
        id=id,
        version="0.1.0",
        content_hash=sha256_digest(()),
        provenance=DatasetProvenance(
            source="Describe the synthetic or redistributable source.",
            license="Declare the dataset license.",
            synthetic=True,
        ),
    )

    def samples(self) -> Sequence[BenchmarkSample]:
        """Return immutable, validated samples after implementing the importer."""
        return ()

    def read_artifact(self, artifact: ModalityArtifact) -> bytes:
        """Return exact source bytes whose SHA-256 digest is artifact.id."""
        raise NotImplementedError(f"Implement artifact reading for {artifact.id}")
'''

_SOLUTIONS_TEMPLATE = '''"""Solution adapters for __SLUG__."""

from datetime import UTC, datetime

from ai_bench.contracts import (
    Compatibility,
    CompatibilityLevel,
    ExecutionOutput,
    ExecutionRequest,
    ParsedOutput,
    PipelineType,
    PricingModel,
    StandalonePipeline,
)


class OfflineSolution:
    id = "__SLUG__-offline"
    version = "0.1.0"
    networked = False
    pipeline = StandalonePipeline(
        pipeline_type=PipelineType.STANDALONE,
        adapter_id=id,
    )
    compatibility = Compatibility(
        level=CompatibilityLevel.FULL,
        comparable_quality=True,
        reason="Update after defining dataset and output semantics.",
    )
    pricing = PricingModel(
        id="__SLUG__-offline-pricing",
        version="0.1.0",
        currency="USD",
        region="offline",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="Explicit pack declaration; update before release.",
        rates=(),
    )

    def execute(self, request: ExecutionRequest) -> ExecutionOutput:
        raise NotImplementedError("Implement provider execution separately from parsing")

    def parse(self, output: ExecutionOutput) -> ParsedOutput:
        raise NotImplementedError("Implement deterministic output parsing")
'''

_EVALUATOR_TEMPLATE = '''"""Evaluator for __SLUG__."""

from collections.abc import Sequence

from ai_bench.contracts import (
    AggregateEvaluation,
    BenchmarkSample,
    EvaluationRecord,
    EvaluationResult,
    MetricDefinition,
    ParsedOutput,
)


class PackEvaluator:
    id = "__SLUG__-evaluator"
    version = "0.1.0"
    metric_definitions: Sequence[MetricDefinition] = ()

    def evaluate(
        self, sample: BenchmarkSample, output: ParsedOutput
    ) -> EvaluationResult:
        raise NotImplementedError("Define task-specific quality evidence")

    def aggregate(
        self, records: Sequence[EvaluationRecord]
    ) -> AggregateEvaluation:
        raise NotImplementedError("Aggregate declared metrics without platform changes")
'''

_VIEWER_TEMPLATE = '''"""Artifact viewer for __SLUG__."""

from collections.abc import Mapping

from ai_bench.contracts import JsonValue, Modality, ModalityArtifact, RendererDescriptor


class PackViewer:
    descriptor = RendererDescriptor(
        id="__SLUG__-text-viewer",
        version="0.1.0",
        modality=Modality.TEXT,
        media_types=("text/plain",),
        title="Benchmark evidence",
    )

    def render(self, artifact: ModalityArtifact) -> Mapping[str, JsonValue]:
        return {
            "supported": artifact.modality is self.descriptor.modality,
            "download": True,
        }
'''

_PACK_TEMPLATE = '''"""Composition root for __SLUG__.

Replace this module's placeholder implementation before using the pack.
"""

from dataclasses import dataclass

from ai_bench.contracts import BenchmarkDefinition, Modality, Terminology

from .dataset import Dataset
from .evaluator import PackEvaluator
from .solutions import OfflineSolution
from .viewer import PackViewer

DEFINITION = BenchmarkDefinition(
    id="__SLUG__",
    version="0.1.0",
    name="__SLUG__",
    description="Replace with the benchmark purpose.",
    input_modalities=(Modality.TEXT,),
    reference_modalities=(Modality.TEXT,),
    terminology=Terminology(
        benchmark_singular="benchmark",
        sample_singular="sample",
        solution_singular="solution",
        reference_singular="reference",
    ),
)


@dataclass(frozen=True)
class Pack:
    definition: BenchmarkDefinition
    dataset: Dataset
    solutions: tuple[OfflineSolution, ...]
    evaluator: PackEvaluator
    viewer: PackViewer


PACK = Pack(
    definition=DEFINITION,
    dataset=Dataset(),
    solutions=(OfflineSolution(),),
    evaluator=PackEvaluator(),
    viewer=PackViewer(),
)


def get_pack() -> Pack:
    return PACK
'''

_TEST_TEMPLATE = '''"""Starter contract test for __SLUG__."""

from ai_bench.packs import validate_pack

from .pack import PACK


def test_pack_contract() -> None:
    validate_pack(PACK)
'''
