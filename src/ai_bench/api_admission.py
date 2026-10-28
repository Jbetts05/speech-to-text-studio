"""Bounded shared short-window backpressure, never a cumulative spend/launch cap."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import Field

from ai_bench.contracts import Contract, utc_now
from ai_bench.storage_state import StateChange, StateConflictError, StateStore


class _AdmissionWindow(Contract):
    minute: int = Field(ge=0)
    count: int = Field(ge=0)


class HttpAdmission:
    def __init__(
        self,
        state: StateStore,
        *,
        clock: Callable[[], datetime] = utc_now,
        per_minute: int = 10,
    ) -> None:
        if per_minute < 1:
            raise ValueError("HTTP admission rate must be positive.")
        self.state = state
        self.clock = clock
        self.per_minute = per_minute

    def admit(self, operation: Literal["run", "upload"]) -> None:
        key = f"http-admission:{operation}"
        for _ in range(20):
            timestamp = int(self.clock().timestamp())
            minute = timestamp // 60
            row = self.state.get(key)
            previous = (
                None if row is None else _AdmissionWindow.model_validate(row.values, strict=False)
            )
            count = previous.count if previous is not None and previous.minute == minute else 0
            if count >= self.per_minute:
                raise HTTPException(
                    status_code=429,
                    detail="The service is busy. Retry the same operation after the delay.",
                    headers={"Retry-After": str(max(1, 60 - timestamp % 60))},
                )
            window = _AdmissionWindow(minute=minute, count=count + 1)
            try:
                self.state.transact(
                    (
                        StateChange(
                            key,
                            window.model_dump(mode="json"),
                            None if row is None else row.etag,
                        ),
                    )
                )
                return
            except StateConflictError:
                continue
        raise StateConflictError("HTTP admission is contended")
