"""Genuine versioned backend qualification, with exact-build publication provenance."""

from __future__ import annotations

import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from pydantic import Field

from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.contracts import Contract
from ai_bench.hashing import sha256_digest
from ai_bench.storage_state import AzureTableStateStore, StateChange, StateConflictError

SUITE_VERSION: Literal["hosted-core-v1"] = "hosted-core-v1"
PROOF_NAMESPACE = "backend-proofs"
REQUIRED_SCENARIOS = frozenset(
    {
        "reservation-replay-conflict",
        "complete-matrix-publication",
        "task-fence-unstarted-release",
        "evidence-first-terminal-execution",
        "event-cursor-terminal-replay",
        "ordinary-run-retention",
        "conditional-state-transactions",
        "large-matrix-crash-repair",
        "ambiguous-response-reconciliation",
        "supervisor-takeover-checkpoint-recovery",
        "indeterminate-attempt-recovery",
        "immutable-object-integrity-generations",
        "expiry-physical-collection",
        "exact-terminal-archive-transfer",
        "shared-object-baseline-pin",
        "failure-cooldown-isolation",
        "finalizer-takeover",
        "bounded-parallel-single-owner",
        "global-solution-lanes-fast-progress",
        "same-run-parallel-evidence-events",
        "eligible-lanes-beyond-first-page",
    }
)
_PROOF_FILES = (
    "artifacts.py",
    "azure_storage.py",
    "azure_artifacts.py",
    "archives.py",
    "backends.py",
    "backend_proof.py",
    "conformance.py",
    "contracts.py",
    "datasets.py",
    "dataset_imports.py",
    "dispatch.py",
    "engine.py",
    "hashing.py",
    "lifecycle.py",
    "packs.py",
    "parallel_conformance.py",
    "receipts.py",
    "reports.py",
    "repositories.py",
    "runtime_policy.py",
    "storage_state.py",
    "table_repository.py",
    "uploads.py",
    "worker.py",
)


def backend_code_signature() -> str:
    root = Path(__file__).resolve().parent
    sources = {
        name: (root / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        for name in _PROOF_FILES
    }
    packages = {}
    for package in (
        "azure-core",
        "azure-data-tables",
        "azure-storage-blob",
        "azure-identity",
        "pydantic",
    ):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = "not-installed"
    return sha256_digest(
        {
            "sources": sources,
            "packages": packages,
            "python": list(sys.version_info[:2]),
        }
    )


class BackendConformanceReceipt(Contract):
    schema_version: Literal["1"] = "1"
    suite_version: Literal["hosted-core-v1"] = SUITE_VERSION
    backend: Literal["azure-table-blob"] = "azure-table-blob"
    identity: Literal["managed-identity"] = "managed-identity"
    code_signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration_signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_namespace: str = Field(pattern=r"^conformance-[A-Za-z0-9_-]+$")
    completed_at: datetime
    scenarios: tuple[str, ...]


class QualificationRequiredError(ValueError):
    """A genuine compatible-format proof lacks the current required scenarios."""


def _proof_store(
    state: AzureTableStateStore, objects: AzureBlobArtifactStore
) -> tuple[AzureTableStateStore, AzureBlobArtifactStore]:
    proof_state = AzureTableStateStore(
        state.client, PROOF_NAMESPACE, match_condition=state.match_condition
    )
    return proof_state, AzureBlobArtifactStore(
        objects.container,
        proof_state,
        objects.cache_root / "proofs",
        object_namespace=PROOF_NAMESPACE,
        match_condition=objects.match_condition,
    )


def publish_receipt(
    state: AzureTableStateStore,
    objects: AzureBlobArtifactStore,
    checkpoint: BackendConformanceReceipt,
) -> str:
    checkpoint = BackendConformanceReceipt.model_validate_json(checkpoint.model_dump_json())
    if (
        checkpoint.source_namespace != state.namespace
        or not REQUIRED_SCENARIOS.issubset(checkpoint.scenarios)
        or checkpoint.code_signature != backend_code_signature()
    ):
        raise ValueError("conformance checkpoint has incomplete or stale evidence")
    proof_state, proof_objects = _proof_store(state, objects)
    artifact = proof_objects.put_json(checkpoint, metadata={"role": "backend-conformance"})
    key = f"checkpoint:{artifact.id}"
    values = {"artifact_id": artifact.id}
    try:
        proof_state.transact((StateChange(key, values),))
    except StateConflictError:
        existing = proof_state.get(key)
        if existing is None or existing.values != values:
            raise
    return artifact.id


def verify_receipt(
    state: AzureTableStateStore,
    objects: AzureBlobArtifactStore,
    digest: str,
    configuration_signature: str,
    *,
    require_current_build: bool = False,
) -> bool:
    if not digest:
        return False
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("backend conformance checkpoint must be a SHA-256 digest")
    proof_state, proof_objects = _proof_store(state, objects)
    marker = proof_state.get(f"checkpoint:{digest}")
    if marker is None or marker.values["artifact_id"] != digest:
        raise ValueError("backend conformance checkpoint has not been published")
    checkpoint = BackendConformanceReceipt.model_validate_json(
        proof_objects.get_bytes(digest), strict=False
    )
    if checkpoint.configuration_signature != configuration_signature:
        raise ValueError("backend proof is incomplete or does not match this deployment")
    if not REQUIRED_SCENARIOS.issubset(checkpoint.scenarios):
        raise QualificationRequiredError("backend proof lacks required qualification scenarios")
    if require_current_build and checkpoint.code_signature != backend_code_signature():
        raise QualificationRequiredError("backend proof does not qualify this exact build")
    return True


def qualification_sources() -> tuple[str, ...]:
    """Release-sensitive sources; changing these requires exact-build conformance."""
    return tuple(f"src/ai_bench/{name}" for name in _PROOF_FILES)
