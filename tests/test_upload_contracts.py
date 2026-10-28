from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_bench.api_contracts import CreateUploadRequest


def _request() -> dict[str, object]:
    return {
        "benchmark_id": "generic_pack",
        "name": "Synthetic upload",
        "language": "en",
        "public_acknowledged": True,
        "rights_acknowledged": True,
        "language_acknowledged": True,
        "files": [{"name": "sample.wav", "size_bytes": 1, "sha256": "0" * 64}],
    }


@pytest.mark.parametrize(
    "field", ["public_acknowledged", "rights_acknowledged", "language_acknowledged"]
)
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_public_acknowledgments_are_explicit_booleans(field: str, value: object) -> None:
    request = {**_request(), field: value}
    with pytest.raises(ValidationError, match="boolean true"):
        CreateUploadRequest.model_validate(request)


def test_aggregate_byte_boundary_is_reserved_before_file_reception() -> None:
    files = [
        {"name": f"sample-{index}.wav", "size_bytes": 25_000_000, "sha256": "0" * 64}
        for index in range(10)
    ]
    parsed = CreateUploadRequest.model_validate({**_request(), "files": files})
    assert sum(part.size_bytes for part in parsed.files) == 250_000_000
    with pytest.raises(ValidationError, match="Total reserved"):
        CreateUploadRequest.model_validate(
            {
                **_request(),
                "files": [*files, {"name": "extra.txt", "size_bytes": 1, "sha256": "1" * 64}],
            }
        )


def test_duplicate_part_names_and_unknown_admin_fields_are_rejected() -> None:
    file = {"name": "same.wav", "size_bytes": 1, "sha256": "0" * 64}
    with pytest.raises(ValidationError, match="unique"):
        CreateUploadRequest.model_validate({**_request(), "files": [file, file]})
    with pytest.raises(ValidationError, match="Extra inputs"):
        CreateUploadRequest.model_validate({**_request(), "pinned": True})


@pytest.mark.parametrize("size", [0, -1, True, 25_000_001])
def test_unbounded_or_coerced_file_sizes_are_rejected(size: object) -> None:
    with pytest.raises(ValidationError):
        CreateUploadRequest.model_validate(
            {
                **_request(),
                "files": [{"name": "sample.wav", "size_bytes": size, "sha256": "0" * 64}],
            }
        )
