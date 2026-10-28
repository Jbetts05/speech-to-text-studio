"""Explicit local/hosted composition without an authoritative hosted filesystem."""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ai_bench.artifacts import ArtifactStore, FileArtifactStore
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.azure_storage import AzureStorageConfig, AzureStorageConfigurationError, _service_url
from ai_bench.backend_proof import PROOF_NAMESPACE, verify_receipt
from ai_bench.datasets import DatasetCatalog
from ai_bench.hashing import sha256_digest
from ai_bench.lifecycle import LifecycleService
from ai_bench.repositories import ExecutionRepository, SqliteRepository
from ai_bench.runtime_policy import PublicExecutionPolicy
from ai_bench.storage_state import (
    AzureTableStateStore,
    PrefixedStateStore,
    SqliteStateStore,
    StateStore,
)
from ai_bench.table_repository import AzureTableRepository


class BackendNotPromotedError(RuntimeError):
    pass


@dataclass
class BackendBundle:
    repository: ExecutionRepository
    artifacts: ArtifactStore
    state: StateStore
    lifecycle: LifecycleService
    datasets: DatasetCatalog
    name: str
    live_proof: bool = False
    staging: ArtifactStore | None = None
    configuration_signature: str = ""
    operator_conformance: bool = False
    proof_error: str | None = None
    proof_verifier: Callable[[], bool] | None = None
    public_policy: PublicExecutionPolicy | None = None

    @contextmanager
    def execution_session(self) -> Iterator[None]:
        if isinstance(self.state, SqliteStateStore):
            with self.state.execution_session():
                yield
        else:
            yield

    def refresh_proof(self) -> None:
        if self.proof_verifier is None:
            return
        try:
            self.live_proof = self.proof_verifier()
            self.proof_error = None if self.live_proof else "Live backend conformance is pending."
        except Exception as error:
            self.live_proof = False
            self.proof_error = f"Backend conformance proof unavailable ({type(error).__name__})."

    def require_execution_ready(self) -> None:
        if (
            self.name == "azure-table-blob"
            and not self.live_proof
            and not self.operator_conformance
        ):
            raise BackendNotPromotedError(
                self.proof_error or "Live backend conformance is pending."
            )

    def assert_ready(self) -> None:
        """Read-only checks for the selected backend, never the legacy Queue wrapper."""
        self.state.page("readiness:", limit=1)
        for store in (self.artifacts, self.staging):
            if isinstance(store, AzureBlobArtifactStore):
                store.container.get_container_properties()
            elif isinstance(store, FileArtifactStore) and not store.objects.is_dir():
                raise OSError("local artifact repository is unavailable")
        self.require_execution_ready()


def local_backend(
    root: Path,
    *,
    durable_projections: bool = False,
    public_policy: PublicExecutionPolicy | None = None,
) -> BackendBundle:
    if os.getenv("AI_BENCH_RUNTIME", "local").lower() == "azure" or os.getenv(
        "WEBSITE_INSTANCE_ID"
    ):
        raise AzureStorageConfigurationError("hosted runtime cannot use local SQLite")
    artifacts = FileArtifactStore(root / "artifacts")
    state = SqliteStateStore(root / "state.sqlite3")
    if durable_projections and state.has_legacy_runs():
        raise AzureStorageConfigurationError(
            "local-durable needs a fresh target; transfer existing SQLite runs through an archive"
        )
    lifecycle = LifecycleService(state, artifacts)
    repository: ExecutionRepository = (
        AzureTableRepository(state, artifacts, lifecycle, public_policy=public_policy)
        if durable_projections
        else SqliteRepository(
            root / "state.sqlite3", lifecycle=lifecycle, public_policy=public_policy
        )
    )
    if isinstance(repository, SqliteRepository):
        lifecycle.descriptor_cleanup = repository.delete_artifact_registration
        lifecycle.external_owner = repository.has_legacy_artifact_owner
        lifecycle.external_root = lambda run_id: repository.get_run(run_id) is not None
    return BackendBundle(
        repository,
        artifacts,
        state,
        lifecycle,
        DatasetCatalog(state, artifacts, lifecycle),
        "local-durable" if durable_projections else "single-instance-sqlite-filesystem",
        staging=FileArtifactStore(root / "staging"),
        public_policy=public_policy,
    )


def azure_backend(
    cache_root: Path,
    *,
    verify_existing_proof: bool = True,
    public_policy: PublicExecutionPolicy | None = None,
) -> BackendBundle:
    """Connect only to preprovisioned resources; identity/configuration never falls back."""
    config = AzureStorageConfig.from_environment()
    if (
        os.getenv("WEBSITE_INSTANCE_ID")
        and os.getenv("AI_BENCH_RUNTIME", "local").lower() != "azure"
    ):
        raise AzureStorageConfigurationError("App Service requires an explicit azure runtime")
    if config.connection_string:
        raise AzureStorageConfigurationError("execution storage requires Entra authentication")
    namespace = os.getenv("AI_BENCH_AZURE_NAMESPACE")
    if not namespace:
        raise AzureStorageConfigurationError("AI_BENCH_AZURE_NAMESPACE is required")
    if namespace == PROOF_NAMESPACE:
        raise AzureStorageConfigurationError(
            "the proof namespace is reserved for operator receipts"
        )
    policy = public_policy or PublicExecutionPolicy()
    table_endpoint = os.getenv("AI_BENCH_AZURE_TABLE_ENDPOINT") or _service_url(
        config.account_url, "table"
    )
    parsed_table = urlsplit(table_endpoint)
    if (
        parsed_table.scheme != "https"
        or not parsed_table.hostname
        or parsed_table.username is not None
        or parsed_table.password is not None
        or parsed_table.query
        or parsed_table.fragment
        or parsed_table.path not in {"", "/"}
        or ".table." not in parsed_table.hostname
        or parsed_table.port not in {None, 443}
    ):
        raise AzureStorageConfigurationError(
            "AI_BENCH_AZURE_TABLE_ENDPOINT must be an HTTPS Table endpoint"
        )
    table_endpoint = table_endpoint.rstrip("/")
    identity = importlib.import_module("azure.identity")
    blob = importlib.import_module("azure.storage.blob")
    tables = importlib.import_module("azure.data.tables")
    if os.getenv("AI_BENCH_RUNTIME", "local").lower() == "azure":
        client_id = os.getenv("AI_BENCH_AZURE_MANAGED_IDENTITY_CLIENT_ID")
        credential = (
            identity.ManagedIdentityCredential(client_id=client_id)
            if client_id
            else identity.ManagedIdentityCredential()
        )
    else:
        credential = identity.DefaultAzureCredential()
    service = blob.BlobServiceClient(
        account_url=_service_url(config.account_url, "blob"), credential=credential
    )
    table = tables.TableClient(
        endpoint=table_endpoint,
        table_name=config.table,
        credential=credential,
    )
    state = AzureTableStateStore(table, namespace)
    artifacts = AzureBlobArtifactStore(
        service.get_container_client(config.artifact_container),
        state,
        cache_root,
        object_namespace=namespace,
    )
    lifecycle = LifecycleService(state, artifacts)
    signature = sha256_digest(
        {
            "blob_endpoint": _service_url(config.account_url, "blob"),
            "table_endpoint": table_endpoint,
            "table": config.table,
            "artifact_container": config.artifact_container,
            "staging_container": os.getenv("AI_BENCH_AZURE_STAGING_CONTAINER", "ai-bench-staging"),
            "runtime": os.getenv("AI_BENCH_RUNTIME", "local").lower(),
            "managed_identity": os.getenv(
                "AI_BENCH_AZURE_MANAGED_IDENTITY_CLIENT_ID", "system-assigned"
            ),
            "public_policy": policy.model_dump(mode="json"),
        }
    )
    receipt_digest = os.getenv("AI_BENCH_BACKEND_CONFORMANCE_RECEIPT", "")
    if receipt_digest and (
        len(receipt_digest) != 64
        or any(character not in "0123456789abcdef" for character in receipt_digest)
    ):
        raise AzureStorageConfigurationError(
            "configured backend conformance checkpoint must be a SHA-256 digest"
        )
    bundle = BackendBundle(
        AzureTableRepository(state, artifacts, lifecycle, public_policy=policy),
        artifacts,
        state,
        lifecycle,
        DatasetCatalog(state, artifacts, lifecycle),
        "azure-table-blob",
        staging=AzureBlobArtifactStore(
            service.get_container_client(
                os.getenv("AI_BENCH_AZURE_STAGING_CONTAINER", "ai-bench-staging")
            ),
            PrefixedStateStore(state, "staging:"),
            cache_root / "staging",
            object_namespace=namespace,
        ),
        configuration_signature=signature,
        operator_conformance=not verify_existing_proof,
        proof_verifier=(
            (lambda: verify_receipt(state, artifacts, receipt_digest, signature))
            if verify_existing_proof
            else None
        ),
        public_policy=policy,
    )
    bundle.refresh_proof()
    return bundle


def configured_backend(
    root: Path, *, public_policy: PublicExecutionPolicy | None = None
) -> BackendBundle:
    selection = os.getenv("AI_BENCH_STORAGE_BACKEND", "local").lower()
    runtime = os.getenv("AI_BENCH_RUNTIME", "local").lower()
    if os.getenv("WEBSITE_INSTANCE_ID") and runtime != "azure":
        raise AzureStorageConfigurationError("App Service must not fall back to local execution")
    if runtime not in {"local", "azure"}:
        raise AzureStorageConfigurationError("runtime must be local or azure")
    hosted = runtime == "azure"
    if selection in {"azure-table", "azure-table-blob"}:
        return azure_backend(root / "cache", public_policy=public_policy)
    if hosted:
        raise AzureStorageConfigurationError("hosted runtime requires azure-table-blob storage")
    if selection == "local":
        return local_backend(root, public_policy=public_policy)
    if selection == "local-durable":
        return local_backend(root, durable_projections=True, public_policy=public_policy)
    raise AzureStorageConfigurationError("unknown execution storage backend")
