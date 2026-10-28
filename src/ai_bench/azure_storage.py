"""Optional Azure Blob, Queue, and Table repository boundary."""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from contextlib import suppress
from typing import Any, cast
from uuid import uuid4

from pydantic import Field, model_validator

from ai_bench.contracts import Artifact, Contract, JsonValue, utc_now
from ai_bench.hashing import (
    canonical_json,
    content_digest,
    normalize_media_type,
    sha256_digest,
)


class AzureStorageConfigurationError(RuntimeError):
    pass


class AzureStorageDependencyError(RuntimeError):
    pass


class AzureStorageUnavailableError(RuntimeError):
    pass


class AzureStorageConfig(Contract):
    account_url: str | None = Field(default=None, pattern=r"^https://[A-Za-z0-9.-]+$")
    connection_string: str | None = Field(default=None, min_length=1)
    artifact_container: str = Field(
        default="ai-bench-artifacts", pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$"
    )
    task_queue: str = Field(
        default="ai-bench-tasks", pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$"
    )
    poison_queue: str = Field(
        default="ai-bench-poison", pattern=r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$"
    )
    table: str = Field(default="aibenchstate", pattern=r"^[A-Za-z][A-Za-z0-9]{2,62}$")

    @model_validator(mode="after")
    def exactly_one_endpoint(self) -> AzureStorageConfig:
        if bool(self.account_url) == bool(self.connection_string):
            raise ValueError("set exactly one of account_url or connection_string")
        if self.account_url and not any(
            marker in self.account_url for marker in (".blob.", ".queue.", ".table.")
        ):
            raise ValueError("account_url must be an Azure Blob, Queue, or Table service URL")
        return self

    @classmethod
    def from_environment(cls) -> AzureStorageConfig:
        account_url = os.getenv("AI_BENCH_AZURE_STORAGE_ACCOUNT_URL")
        connection_string = os.getenv("AI_BENCH_AZURE_STORAGE_CONNECTION_STRING")
        if not account_url and not connection_string:
            raise AzureStorageConfigurationError(
                "Azure Storage is not configured. Set "
                "AI_BENCH_AZURE_STORAGE_ACCOUNT_URL for identity-based access, or "
                "AI_BENCH_AZURE_STORAGE_CONNECTION_STRING explicitly."
            )
        return cls(
            account_url=account_url,
            connection_string=connection_string,
            artifact_container=os.getenv("AI_BENCH_AZURE_ARTIFACT_CONTAINER", "ai-bench-artifacts"),
            task_queue=os.getenv("AI_BENCH_AZURE_TASK_QUEUE", "ai-bench-tasks"),
            poison_queue=os.getenv("AI_BENCH_AZURE_POISON_QUEUE", "ai-bench-poison"),
            table=os.getenv("AI_BENCH_AZURE_STATE_TABLE", "aibenchstate"),
        )


class AzureQueueMessage(Contract):
    message_id: str
    pop_receipt: str
    content: str
    dequeue_count: int = Field(ge=0)


class AzureStorageRepositories:
    """Usable optional storage boundary without an import-time Azure dependency.

    Blob stores immutable artifact bytes, Queue carries task identities, and Table
    stores artifact metadata, run records, events, and idempotent dispatch state.
    """

    def __init__(self, config: AzureStorageConfig | None = None) -> None:
        self.config = config or AzureStorageConfig.from_environment()
        runtime = os.getenv("AI_BENCH_RUNTIME", "local").lower()
        if runtime not in {"local", "azure"}:
            raise AzureStorageConfigurationError(
                "AI_BENCH_RUNTIME must be either 'local' or 'azure'"
            )
        if runtime == "azure" and self.config.connection_string:
            raise AzureStorageConfigurationError(
                "storage connection strings are forbidden when AI_BENCH_RUNTIME=azure; "
                "configure AI_BENCH_AZURE_STORAGE_ACCOUNT_URL for Entra authentication"
            )
        blob_module, queue_module, table_module, identity_module = _azure_modules()
        self._resource_exists = _resource_exists_exception()
        if self.config.connection_string:
            connection_string = self.config.connection_string
            self._blob_service = blob_module.BlobServiceClient.from_connection_string(
                connection_string
            )
            self._queue_service = queue_module.QueueServiceClient.from_connection_string(
                connection_string
            )
            self._table_service = table_module.TableServiceClient.from_connection_string(
                connection_string
            )
        else:
            if runtime == "azure":
                client_id = os.getenv("AI_BENCH_AZURE_MANAGED_IDENTITY_CLIENT_ID")
                credential = (
                    identity_module.ManagedIdentityCredential(client_id=client_id)
                    if client_id
                    else identity_module.ManagedIdentityCredential()
                )
            else:
                credential = identity_module.DefaultAzureCredential()
            self._blob_service = blob_module.BlobServiceClient(
                account_url=_service_url(self.config.account_url, "blob"),
                credential=credential,
            )
            self._queue_service = queue_module.QueueServiceClient(
                account_url=_service_url(self.config.account_url, "queue"),
                credential=credential,
            )
            self._table_service = table_module.TableServiceClient(
                endpoint=_service_url(self.config.account_url, "table"),
                credential=credential,
            )
        self._container = self._blob_service.get_container_client(self.config.artifact_container)
        self._tasks = self._queue_service.get_queue_client(self.config.task_queue)
        self._poison = self._queue_service.get_queue_client(self.config.poison_queue)
        self._table = self._table_service.get_table_client(self.config.table)

    def initialize(self) -> None:
        """Create the configured resources if absent."""
        for create in (
            self._container.create_container,
            self._tasks.create_queue,
            self._poison.create_queue,
            self._table.create_table,
        ):
            with suppress(self._resource_exists):
                create()

    def assert_ready(self) -> None:
        """Raise with context unless Blob, Queue, and Table are all reachable."""
        try:
            self._container.get_container_properties()
            self._tasks.get_queue_properties()
            self._poison.get_queue_properties()
            entities = self._table.query_entities(
                query_filter="PartitionKey eq '__ai_bench_readiness_probe__'",
                select=["PartitionKey"],
                results_per_page=1,
            )
            next(iter(entities), None)
        except Exception as error:
            raise AzureStorageUnavailableError(
                "configured Azure Blob, Queue, or Table dependency is not ready"
            ) from error

    def is_ready(self) -> bool:
        try:
            self.assert_ready()
        except AzureStorageUnavailableError:
            return False
        return True

    def put_artifact(self, artifact: Artifact, content: bytes) -> None:
        if content_digest(content) != artifact.id:
            raise ValueError("artifact content does not match its SHA-256 identifier")
        blob = self._container.get_blob_client(f"sha256/{artifact.id[:2]}/{artifact.id}")
        try:
            blob.upload_blob(
                content,
                overwrite=False,
                content_settings=_content_settings(artifact.media_type),
                metadata={"sha256": artifact.id},
            )
        except self._resource_exists as error:
            existing = blob.get_blob_properties()
            if int(existing.size) != len(content):
                raise AzureStorageUnavailableError(
                    "existing content-addressed blob has an unexpected size"
                ) from error
        self._table.upsert_entity(
            {
                "PartitionKey": f"artifact-variant:{artifact.id}",
                "RowKey": sha256_digest(artifact),
                "ArtifactJson": artifact.model_dump_json(),
            },
            mode="merge",
        )
        current = self.get_artifact(artifact.id)
        if current is not None and (
            normalize_media_type(current.media_type),
            current.modality.value,
        ) <= (
            normalize_media_type(artifact.media_type),
            artifact.modality.value,
        ):
            return
        self._table.upsert_entity(
            {
                "PartitionKey": "artifact",
                "RowKey": artifact.id,
                "ArtifactJson": artifact.model_dump_json(),
            },
            mode="merge",
        )

    def get_artifact_bytes(self, artifact_id: str) -> bytes:
        _validate_digest(artifact_id)
        blob = self._container.get_blob_client(f"sha256/{artifact_id[:2]}/{artifact_id}")
        content = bytes(blob.download_blob().readall())
        if content_digest(content) != artifact_id:
            raise AzureStorageUnavailableError("downloaded artifact failed SHA-256 validation")
        return content

    def get_artifact(
        self,
        artifact_id: str,
        media_type: str | None = None,
    ) -> Artifact | None:
        _validate_digest(artifact_id)
        partition_key = "artifact"
        row_key = artifact_id
        if media_type is not None:
            partition_key = f"artifact-variant:{artifact_id}"
            entities = self._table.query_entities(
                query_filter=f"PartitionKey eq '{partition_key}'",
            )
            matches = sorted(
                (
                    Artifact.model_validate_json(entity["ArtifactJson"], strict=False)
                    for entity in entities
                    if str(entity.get("ArtifactJson", ""))
                ),
                key=lambda artifact: (
                    artifact.modality.value,
                    canonical_json(artifact),
                ),
            )
            return next(
                (
                    artifact
                    for artifact in matches
                    if normalize_media_type(artifact.media_type) == normalize_media_type(media_type)
                ),
                None,
            )
        try:
            entity = self._table.get_entity(
                partition_key=partition_key,
                row_key=row_key,
            )
        except Exception as error:
            if _is_not_found(error):
                return None
            raise
        return Artifact.model_validate_json(entity["ArtifactJson"], strict=False)

    def put_run_record(self, run_id: str, payload: Mapping[str, JsonValue]) -> None:
        _validate_digest(run_id)
        self._table.upsert_entity(
            {
                "PartitionKey": "run",
                "RowKey": run_id,
                "PayloadJson": canonical_json(dict(payload)).decode("ascii"),
                "UpdatedAt": utc_now().isoformat(),
            },
            mode="replace",
        )

    def get_run_record(self, run_id: str) -> dict[str, JsonValue] | None:
        _validate_digest(run_id)
        try:
            entity = self._table.get_entity(partition_key="run", row_key=run_id)
        except Exception as error:
            if _is_not_found(error):
                return None
            raise
        import json

        value = json.loads(str(entity["PayloadJson"]))
        if not isinstance(value, dict):
            raise AzureStorageUnavailableError("Azure run record is not a JSON object")
        return value

    def enqueue_task(self, task_id: str, payload: Mapping[str, JsonValue]) -> bool:
        """Enqueue once using a Table dispatch record as the idempotency gate."""
        _validate_digest(task_id)
        body = canonical_json({"task_id": task_id, "payload": dict(payload)}).decode("ascii")
        entity = {
            "PartitionKey": "dispatch",
            "RowKey": task_id,
            "State": "dispatching",
            "PayloadJson": body,
            "UpdatedAt": utc_now().isoformat(),
        }
        try:
            self._table.create_entity(entity)
        except self._resource_exists:
            return False
        try:
            self._tasks.send_message(body)
            entity["State"] = "sent"
            entity["UpdatedAt"] = utc_now().isoformat()
            self._table.update_entity(entity, mode="replace")
        except Exception:
            self._table.delete_entity(partition_key="dispatch", row_key=task_id)
            raise
        return True

    def receive_tasks(
        self, *, max_messages: int = 16, visibility_timeout: int = 60
    ) -> tuple[AzureQueueMessage, ...]:
        if not 1 <= max_messages <= 32:
            raise ValueError("max_messages must be between 1 and 32")
        if not 1 <= visibility_timeout <= 7 * 24 * 60 * 60:
            raise ValueError("visibility_timeout must be between 1 second and 7 days")
        messages = self._tasks.receive_messages(
            messages_per_page=max_messages,
            visibility_timeout=visibility_timeout,
        )
        result: list[AzureQueueMessage] = []
        for message in messages:
            result.append(
                AzureQueueMessage(
                    message_id=str(message.id),
                    pop_receipt=str(message.pop_receipt),
                    content=str(message.content),
                    dequeue_count=int(message.dequeue_count or 0),
                )
            )
            if len(result) == max_messages:
                break
        return tuple(result)

    def complete_task(self, message: AzureQueueMessage) -> None:
        self._tasks.delete_message(message.message_id, message.pop_receipt)

    def poison_task(self, message: AzureQueueMessage, reason: str) -> None:
        body = canonical_json(
            {
                "original_message_id": message.message_id,
                "content": message.content,
                "reason": reason[:1000],
                "poisoned_at": utc_now(),
            }
        ).decode("ascii")
        self._poison.send_message(body)
        self.complete_task(message)

    def append_event(self, run_id: str, event_type: str, payload: Mapping[str, JsonValue]) -> str:
        _validate_digest(run_id)
        event_id = f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}"
        self._table.create_entity(
            {
                "PartitionKey": f"event:{run_id}",
                "RowKey": event_id,
                "EventType": event_type,
                "PayloadJson": canonical_json(dict(payload)).decode("ascii"),
            }
        )
        return event_id


def _azure_modules() -> tuple[Any, Any, Any, Any]:
    try:
        return (
            importlib.import_module("azure.storage.blob"),
            importlib.import_module("azure.storage.queue"),
            importlib.import_module("azure.data.tables"),
            importlib.import_module("azure.identity"),
        )
    except ImportError as error:
        raise AzureStorageDependencyError(
            "Azure Storage support requires the optional dependencies. "
            "Install this project with the 'azure' extra."
        ) from error


def _content_settings(media_type: str) -> Any:
    blob_module = importlib.import_module("azure.storage.blob")
    return blob_module.ContentSettings(content_type=media_type)


def _resource_exists_exception() -> type[Exception]:
    try:
        exception_type = importlib.import_module("azure.core.exceptions").ResourceExistsError
    except ImportError as error:
        raise AzureStorageDependencyError(
            "Azure Storage support requires the optional dependencies. "
            "Install this project with the 'azure' extra."
        ) from error
    return cast(type[Exception], exception_type)


def _is_not_found(error: Exception) -> bool:
    return type(error).__name__ == "ResourceNotFoundError"


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("identifier must be a lowercase SHA-256 digest")


def _service_url(account_url: str | None, service: str) -> str:
    if account_url is None:
        raise AzureStorageConfigurationError("identity-based access requires account_url")
    for marker in (".blob.", ".queue.", ".table."):
        if marker in account_url:
            return account_url.replace(marker, f".{service}.", 1)
    raise AzureStorageConfigurationError("account_url is not an Azure Storage service URL")
