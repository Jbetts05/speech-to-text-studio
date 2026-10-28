from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from ai_bench.api import create_app
from ai_bench.api_contracts import CatalogSolution
from ai_bench.contracts import Compatibility, CompatibilityLevel, PipelineType, StandalonePipeline
from benchmark_packs.speech_to_text.adapters import ManagedTranscriptionSolution


@pytest.mark.parametrize("value", [None, "", "   ", 42, {"title": "not a label"}, ["not a label"]])
def test_unknown_display_metadata_keeps_identity(value: object) -> None:
    solution = CatalogSolution.model_validate(
        {
            "id": "exact-stable-ID",
            "display_name": value,
            "networked": False,
            "pipeline": StandalonePipeline(
                pipeline_type=PipelineType.STANDALONE, adapter_id="adapter"
            ),
            "compatibility": Compatibility(
                level=CompatibilityLevel.FULL, comparable_quality=True, reason="Synthetic fixture"
            ),
        }
    )
    assert solution.display_name is None
    assert solution.id == "exact-stable-ID"


def test_catalog_names_are_inert_and_legacy_pack_remains_discoverable(
    tmp_path: Path, pack_root: Path
) -> None:
    with (
        patch.object(
            ManagedTranscriptionSolution,
            "_configuration",
            side_effect=AssertionError("provider read"),
        ),
        patch.object(
            ManagedTranscriptionSolution,
            "execution_settings",
            side_effect=AssertionError("settings"),
        ),
        patch.object(
            ManagedTranscriptionSolution, "execute", side_effect=AssertionError("provider call")
        ),
    ):
        client = TestClient(create_app(tmp_path / "state", pack_root))
        response = client.get("/api/benchmarks")
    assert response.status_code == 200
    catalog = {item["definition"]["id"]: item for item in response.json()}
    assert {
        solution["id"]: solution["display_name"]
        for solution in catalog["speech_to_text"]["solutions"]
    } == {
        "azure-speech-fast": "Azure Speech Fast",
        "gpt-transcribe": "GPT-Transcribe",
        "managed-whisper": "Managed Whisper",
        "mai-transcribe-1-5": "MAI-Transcribe-1.5",
        "mai-transcribe-2": "MAI-Transcribe-2",
    }
    assert all(
        solution["display_name"] is None
        for solution in catalog["synthetic_text_intent"]["solutions"]
    )
    assert all(solution["networked"] for solution in catalog["speech_to_text"]["solutions"])
