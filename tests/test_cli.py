from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_bench.cli import app


def test_validate_discovers_pack_outside_checkout_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["validate"])

    assert result.exit_code == 0
    assert "Validated 2 benchmark pack(s)." in result.stdout
