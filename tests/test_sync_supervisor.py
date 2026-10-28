from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ai_bench import engine as engine_module
from ai_bench.backends import local_backend
from ai_bench.contracts import RunReport, utc_now
from ai_bench.engine import BenchmarkEngine
from ai_bench.reports import build_report
from ai_bench.repositories import LeaseLostError
from ai_bench.table_repository import AzureTableRepository


@pytest.fixture(params=[False, True], ids=["sqlite", "table"])
def controlled(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    pack_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BenchmarkEngine, list[datetime]]:
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=request.param)
    )
    now = [utc_now()]
    if isinstance(engine.repository, AzureTableRepository):
        engine.repository.clock = lambda: now[0]
    else:
        monkeypatch.setattr("ai_bench.repositories.time.time", lambda: now[0].timestamp())
    engine.lifecycle.clock = lambda: now[0]
    return engine, now


def test_renew_only_rejects_expiry_and_same_owner_replacement(
    controlled: tuple[BenchmarkEngine, list[datetime]],
) -> None:
    engine, now = controlled
    repo = engine.repository
    assert repo.renew_supervisor("owner") is None
    assert repo.acquire_supervisor("owner")
    first = repo.renew_supervisor("owner")
    assert first is not None
    now[0] += timedelta(seconds=31)
    assert repo.renew_supervisor("owner", generation=first) is None
    assert repo.acquire_supervisor("owner")
    replacement = repo.renew_supervisor("owner")
    assert replacement is not None and replacement > first
    assert repo.renew_supervisor("owner", generation=first) is None
    repo.release_supervisor("owner", generation=first)
    assert repo.renew_supervisor("owner", generation=replacement) == replacement
    repo.release_supervisor("owner", generation=replacement)
    assert repo.renew_supervisor("owner", generation=replacement) is None


@pytest.mark.parametrize("replacement_owner", ["owner", "replacement"])
def test_context_loses_admission_without_releasing_replacement(
    controlled: tuple[BenchmarkEngine, list[datetime]],
    replacement_owner: str,
) -> None:
    engine, now = controlled
    replacement = None
    with (
        pytest.raises(LeaseLostError, match="renewal lost"),
        engine.supervisor_session("owner") as allowed,
    ):
        assert allowed()
        now[0] += timedelta(seconds=31)
        assert engine.repository.acquire_supervisor(replacement_owner)
        replacement = engine.repository.renew_supervisor(replacement_owner)
        assert replacement is not None
        assert not allowed()
    assert (
        engine.repository.renew_supervisor(replacement_owner, generation=replacement) == replacement
    )


def test_context_reports_renewal_errors_and_cleans_owned_generation(
    controlled: tuple[BenchmarkEngine, list[datetime]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _ = controlled
    renew = engine.repository.renew_supervisor

    def unavailable(*_: Any, **__: Any) -> int | None:
        raise OSError("synthetic renewal outage")

    with (
        pytest.raises(LeaseLostError) as caught,
        engine.supervisor_session("owner") as allowed,
    ):
        assert allowed()
        monkeypatch.setattr(engine.repository, "renew_supervisor", unavailable)
        assert not allowed()
    assert isinstance(caught.value.__cause__, OSError)
    assert renew("owner") is None


def test_periodic_renewal_covers_slow_finalization_and_replay_is_read_only(
    controlled: tuple[BenchmarkEngine, list[datetime]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, now = controlled
    pulse, renewed = threading.Event(), threading.Event()

    class ControlledEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            if threading.current_thread().name == "ai-bench-sync-supervisor":
                pulse.wait()
                pulse.clear()
                return self.is_set()
            return super().wait(timeout)

        def set(self) -> None:
            super().set()
            pulse.set()

    monkeypatch.setattr(threading, "Event", ControlledEvent)
    renew = engine.repository.renew_supervisor
    generations: list[int] = []

    def observe_renewal(
        owner: str, *, generation: int | None = None, lease_seconds: int = 30
    ) -> int | None:
        result = renew(owner, generation=generation, lease_seconds=lease_seconds)
        if threading.current_thread().name == "ai-bench-sync-supervisor" and result is not None:
            assert lease_seconds == 30
            generations.append(result)
            renewed.set()
        return result

    monkeypatch.setattr(engine.repository, "renew_supervisor", observe_renewal)

    def slow_report(*args: Any, **kwargs: Any) -> RunReport:
        for _ in range(3):
            renewed.clear()
            now[0] += timedelta(seconds=20)
            pulse.set()
            assert renewed.wait(2), "heartbeat must renew while report construction is blocked"
        return build_report(*args, **kwargs)

    monkeypatch.setattr(engine_module, "build_report", slow_report)
    manifest = engine.create_run("synthetic_text_intent", solution_ids=("keyword-standalone",))
    report = engine.run_to_completion(manifest.run_id, worker_id="owner")
    assert report.status.value == "completed"
    assert generations and set(generations) == {1}
    assert renew("owner") is None
    monkeypatch.setattr(
        engine.repository,
        "acquire_supervisor",
        lambda *_args, **_kwargs: pytest.fail("terminal replay must not acquire a supervisor"),
    )
    assert engine.run_to_completion(manifest.run_id) == report
