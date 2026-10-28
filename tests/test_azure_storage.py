from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ai_bench.azure_storage import AzureStorageRepositories


class _PropertiesClient:
    def __init__(self) -> None:
        self.calls = 0

    def get_container_properties(self) -> dict[str, object]:
        self.calls += 1
        return {}

    def get_queue_properties(self) -> dict[str, object]:
        self.calls += 1
        return {}


class _EmptyTable:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    def query_entities(self, **kwargs: Any) -> Iterator[dict[str, object]]:
        self.calls += 1
        assert kwargs == {
            "query_filter": "PartitionKey eq '__ai_bench_readiness_probe__'",
            "select": ["PartitionKey"],
            "results_per_page": 1,
        }
        if self.calls <= self.failures:
            raise PermissionError("role assignment has not propagated")
        return iter(())


def _repository(table: _EmptyTable) -> AzureStorageRepositories:
    repository = object.__new__(AzureStorageRepositories)
    repository._container = _PropertiesClient()
    repository._tasks = _PropertiesClient()
    repository._poison = _PropertiesClient()
    repository._table = table
    return repository


def test_readiness_queries_an_empty_table_without_a_seeded_entity() -> None:
    table = _EmptyTable()
    repository = _repository(table)
    repository.assert_ready()
    assert table.calls == 1
    assert repository._container.calls == 1
    assert repository._tasks.calls == 1
    assert repository._poison.calls == 1


def test_readiness_recovers_after_delayed_rbac_propagation() -> None:
    table = _EmptyTable(failures=1)
    repository = _repository(table)
    assert repository.is_ready() is False
    assert repository.is_ready() is True
    assert table.calls == 2
