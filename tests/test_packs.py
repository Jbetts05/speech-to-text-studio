from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ai_bench.packs import PackValidationError, discover_packs
from ai_bench.scaffold import BriefValidationError, scaffold_pack, validate_brief


def test_synthetic_pack_is_discoverable(pack_root: Path) -> None:
    packs = discover_packs(pack_root)
    assert set(packs) == {"synthetic_text_intent", "speech_to_text"}
    pack = packs["synthetic_text_intent"].pack
    assert len(pack.dataset.samples()) == 6
    assert {solution.pipeline.pipeline_type.value for solution in pack.solutions} == {
        "standalone",
        "assisted",
    }
    assert all(not solution.networked for solution in pack.solutions)
    assert all(solution.pricing.version for solution in pack.solutions)
    assert packs["synthetic_text_intent"].config.offline_default is True
    assert packs["synthetic_text_intent"].config.networked_execution is False


def test_scaffold_never_overwrites_and_validates_brief(tmp_path: Path) -> None:
    brief = tmp_path / "BENCHMARK.md"
    brief.write_text(
        "\n".join(
            [
                "# Complete brief",
                "## Identity",
                "Complete",
                "## Decision this benchmark supports",
                "Complete",
                "## Modality",
                "Complete",
                "## Dataset",
                "Complete",
                "## Solutions",
                "Complete",
                "## Evaluation",
                "Complete",
                "## Compatibility",
                "Complete",
                "## Evidence and reports",
                "Complete",
                "## Runtime and durability",
                "Complete",
                "## Azure deployment",
                "Complete",
                "## Acceptance cases",
                "Complete",
                "## Out of scope",
                "Complete",
            ]
        ),
        encoding="ascii",
    )
    validate_brief(brief)
    destination = scaffold_pack("new_generic_pack", tmp_path / "packs", brief)
    assert {path.name for path in destination.iterdir()} == {
        "__init__.py",
        "config.json",
        "dataset.py",
        "evaluator.py",
        "pack.py",
        "solutions.py",
        "test_pack.py",
        "viewer.py",
    }
    assert '"benchmark_id": "new_generic_pack"' in (destination / "config.json").read_text(
        encoding="ascii"
    )
    generated_test = (destination / "test_pack.py").read_text(encoding="ascii")
    assert "validate_pack(PACK)" in generated_test
    lint = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr
    with pytest.raises(PackValidationError, match="dataset must contain samples"):
        discover_packs(tmp_path / "packs")
    with pytest.raises(FileExistsError):
        scaffold_pack("new_generic_pack", tmp_path / "packs", brief)


def test_brief_rejects_unresolved_markers(tmp_path: Path) -> None:
    brief = tmp_path / "BENCHMARK.md"
    brief.write_text("## Identity\n{{NAME}}\n", encoding="ascii")
    with pytest.raises(BriefValidationError):
        validate_brief(brief)


def test_brief_requires_azure_deployment_section(tmp_path: Path) -> None:
    brief = tmp_path / "BENCHMARK.md"
    brief.write_text(
        "\n".join(
            [
                "## Identity",
                "## Decision this benchmark supports",
                "## Modality",
                "## Dataset",
                "## Solutions",
                "## Evaluation",
                "## Compatibility",
                "## Evidence and reports",
                "## Runtime and durability",
                "## Acceptance cases",
                "## Out of scope",
            ]
        ),
        encoding="ascii",
    )
    with pytest.raises(BriefValidationError, match="## Azure deployment"):
        validate_brief(brief)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not-json", "invalid pack config"),
        (
            json.dumps(
                {
                    "schema_version": "1",
                    "benchmark_id": "different_benchmark",
                    "offline_default": True,
                    "networked_execution": False,
                }
            ),
            "does not match definition id",
        ),
        (
            json.dumps(
                {
                    "schema_version": "1",
                    "benchmark_id": "synthetic_text_intent",
                    "offline_default": True,
                    "networked_execution": False,
                    "unexpected": "rejected",
                }
            ),
            "extra_forbidden",
        ),
    ],
)
def test_pack_config_is_strictly_validated(
    content: str,
    message: str,
    tmp_path: Path,
    pack_root: Path,
) -> None:
    copied_root = tmp_path / "packs"
    destination = copied_root / "synthetic_text_intent"
    shutil.copytree(pack_root / "synthetic_text_intent", destination)
    (destination / "config.json").write_text(content, encoding="ascii")
    with pytest.raises(PackValidationError, match=message):
        discover_packs(copied_root)


def test_pack_config_is_required(tmp_path: Path, pack_root: Path) -> None:
    copied_root = tmp_path / "packs"
    destination = copied_root / "synthetic_text_intent"
    shutil.copytree(pack_root / "synthetic_text_intent", destination)
    (destination / "config.json").unlink()
    with pytest.raises(PackValidationError, match="missing required config"):
        discover_packs(copied_root)
