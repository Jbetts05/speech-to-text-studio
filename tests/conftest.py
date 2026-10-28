from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def pack_root() -> Path:
    return Path(__file__).resolve().parents[1] / "benchmark_packs"
