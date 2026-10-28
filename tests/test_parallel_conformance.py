from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_bench.backends import local_backend
from ai_bench.contracts import TaskResult
from ai_bench.engine import BenchmarkEngine
from ai_bench.parallel_conformance import (
    DRAIN_SECONDS,
    OBSERVATION_SECONDS,
    _remaining,
    parallel_execution_scenarios,
)
from ai_bench.repositories import Lease


def test_lane_waits_have_controller_owned_lifetimes(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane_waits: list[float | None] = []

    class ObservedEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            if threading.current_thread().name.startswith("ai-bench-lane"):
                lane_waits.append(timeout)
                assert timeout is None, "a lane must not expire its own controller-held barrier"
            return super().wait(timeout)

    monkeypatch.setattr(threading, "Event", ObservedEvent)
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    scenarios = parallel_execution_scenarios(engine, "synthetic_text_intent", "keyword-standalone")
    assert "global-solution-lanes-fast-progress" in scenarios
    assert len(lane_waits) >= 24 and set(lane_waits) == {None}


@pytest.mark.parametrize("late_callback", [False, True])
def test_watchdog_cleanup_cannot_satisfy_fast_progress(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch, late_callback: bool
) -> None:
    fire: list[Callable[[], None]] = []
    budgets: list[float] = []

    class ControlledWatchdog:
        daemon = False

        def __init__(self, interval: float, function: Callable[[], None]) -> None:
            budgets.append(interval)
            fire.append(function)

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            pass

        def join(self, timeout: float | None = None) -> None:
            pass

    monkeypatch.setattr(threading, "Timer", ControlledWatchdog)
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    complete = engine.repository.complete_attempt
    count = 0
    lock = threading.Lock()
    monotonic = time.monotonic

    def expire_at_progress(lease: Lease, result: TaskResult) -> None:
        nonlocal count
        complete(lease, result)
        with lock:
            count += 1
            if count == 2:
                if late_callback:
                    monkeypatch.setattr(
                        "ai_bench.parallel_conformance.time.monotonic", lambda: monotonic() + 121
                    )
                else:
                    fire[0]()

    monkeypatch.setattr(engine.repository, "complete_attempt", expire_at_progress)
    with pytest.raises(AssertionError, match="progress|watchdog|slow invocation"):
        parallel_execution_scenarios(engine, "synthetic_text_intent", "keyword-standalone")
    assert count >= 2
    assert len(budgets) == 1 and 0 < budgets[0] <= OBSERVATION_SECONDS


def test_drain_phase_still_guards_against_a_hang(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ai_bench.parallel_conformance.DRAIN_SECONDS", 0.5)
    engine = BenchmarkEngine(
        tmp_path, pack_root, backend=local_backend(tmp_path, durable_projections=True)
    )
    finalize = engine._finalize

    def stalled(run_id: str) -> object:
        time.sleep(3)
        return finalize(run_id)

    monkeypatch.setattr(engine, "_finalize", stalled)
    with pytest.raises(AssertionError, match="terminal reports"):
        parallel_execution_scenarios(engine, "synthetic_text_intent", "keyword-standalone")


def test_concurrency_observation_uses_one_nonrenewable_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("ai_bench.parallel_conformance.time.monotonic", lambda: now[0])
    deadline = now[0] + OBSERVATION_SECONDS
    assert _remaining(deadline) == 120
    now[0] += 31
    assert _remaining(deadline) == 89
    now[0] += 89
    assert _remaining(deadline) == 0
    now[0] += 1
    assert _remaining(deadline) == 0
    # Draining durable evidence must not inherit the concurrency budget.
    assert DRAIN_SECONDS > OBSERVATION_SECONDS
