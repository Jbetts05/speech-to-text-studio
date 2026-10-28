"""Durable response receipts separate physical calls from persistence retries."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import Field

from ai_bench.artifacts import ArtifactStore
from ai_bench.contracts import (
    Artifact,
    Contract,
    Modality,
    ProviderFailureEvidence,
    TelemetryEvidence,
)
from ai_bench.hashing import canonical_json, content_digest
from ai_bench.lifecycle import LifecycleService
from ai_bench.repositories import Lease


class AttemptPersistencePendingError(RuntimeError):
    pass


class UncertainDispatchError(RuntimeError):
    pass


class ProviderReceipt(Contract):
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    task_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    attempt_number: int | None = Field(default=None, ge=1)
    raw: Artifact | None
    telemetry: TelemetryEvidence
    failure: ProviderFailureEvidence | None = None


def encode_receipt(checkpoint: ProviderReceipt, raw: bytes | None) -> bytes:
    return canonical_json(
        {
            "schema_version": "1",
            "checkpoint": checkpoint,
            "raw_base64": None if raw is None else base64.b64encode(raw).decode("ascii"),
        }
    )


def decode_receipt(content: bytes) -> tuple[ProviderReceipt, bytes | None, bytes]:
    document = json.loads(content)
    if "checkpoint" not in document:
        return ProviderReceipt.model_validate_json(content, strict=False), None, content
    if document.get("schema_version") != "1":
        raise ValueError("unsupported durable response checkpoint")
    receipt_bytes = canonical_json(document["checkpoint"])
    checkpoint = ProviderReceipt.model_validate_json(receipt_bytes, strict=False)
    encoded = document["raw_base64"]
    raw = None
    if encoded is not None:
        if not isinstance(encoded, str) or len(encoded) > 90 * 1024 * 1024:
            raise ValueError("response checkpoint raw content exceeds its bound")
        raw = base64.b64decode(encoded, validate=True)
        if (
            checkpoint.raw is None
            or len(raw) != checkpoint.raw.size_bytes
            or content_digest(raw) != checkpoint.raw.id
        ):
            raise ValueError("response checkpoint raw bytes do not match their descriptor")
    return checkpoint, raw, receipt_bytes


def materialize_receipt(
    store: ArtifactStore,
    lifecycle: LifecycleService,
    register: Callable[[Artifact], None],
    run_id: str,
    task_id: str,
    attempt: int,
    content: bytes,
) -> str:
    checkpoint, raw, receipt_bytes = decode_receipt(content)
    if (
        checkpoint.run_id not in {None, run_id}
        or checkpoint.task_id not in {None, task_id}
        or checkpoint.attempt_number not in {None, attempt}
    ):
        raise ValueError("durable checkpoint belongs to another attempt")
    owned = lifecycle.get(run_id) is not None
    if raw is not None:
        assert checkpoint.raw is not None
        if owned:
            lifecycle.attach(run_id, checkpoint.raw.id)
        artifact = store.put_bytes(
            raw,
            modality=checkpoint.raw.modality,
            media_type=checkpoint.raw.media_type,
            metadata=dict(checkpoint.raw.metadata),
        )
        register(artifact)
    digest = content_digest(receipt_bytes)
    if owned:
        lifecycle.attach(run_id, digest)
    document = store.put_bytes(
        receipt_bytes,
        modality=Modality.STRUCTURED,
        media_type="application/json",
        metadata={"role": "response-checkpoint"},
    )
    register(document)
    return digest


@runtime_checkable
class ResponseReceiptRepository(Protocol):
    def begin_dispatch(self, lease: Lease) -> None: ...

    def response_receipt(self, lease: Lease) -> str | None: ...

    def record_response_receipt(self, lease: Lease, artifact_id: str) -> None: ...
