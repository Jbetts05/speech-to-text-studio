"""Trusted composition-time limits, enforced inside durable core transitions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ai_bench.contracts import Contract


class PublicExecutionPolicy(Contract):
    schema_version: Literal["1"] = "1"
    retention_days: Literal[10] = 10
    staging_hours: Literal[24] = 24
    upload_session_seconds: Literal[3600] = 3600
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_active_runs: int = Field(default=100, ge=1, le=100)
    max_outstanding_tasks: int = Field(default=5000, ge=1, le=5000)
    max_upload_sessions: int = Field(default=20, ge=1, le=20)
    max_reserved_upload_bytes: int = Field(default=1_000_000_000, ge=1, le=1_000_000_000)
    max_receivers: int = Field(default=2, ge=1, le=2)
