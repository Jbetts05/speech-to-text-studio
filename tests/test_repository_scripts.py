from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from infra.scripts import stage_package
from scripts import check_wheel, generate_api_types, sync_frontend_assets
from scripts.package_policy import external_payload_name


def test_frontend_asset_sync_is_reproducible_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "frontend" / "dist"
    target = tmp_path / "src" / "ai_bench" / "web" / "static"
    source.mkdir(parents=True)
    (source / "index.html").write_text("first build", encoding="ascii")
    monkeypatch.setattr(sync_frontend_assets, "ROOT", tmp_path)
    monkeypatch.setattr(sync_frontend_assets, "SOURCE", source)
    monkeypatch.setattr(sync_frontend_assets, "TARGET", target)
    monkeypatch.setattr(sync_frontend_assets, "EXPECTED_TARGET", target)

    assert sync_frontend_assets.main() == 0
    assert (target / "index.html").read_text(encoding="ascii") == "first build"

    (source / "index.html").write_bytes(b"first\r\r\nsecond\r\n")
    assert sync_frontend_assets.main() == 0
    assert (target / "index.html").read_bytes() == b"first\nsecond\n"

    (source / "index.html").write_text("unsafe build", encoding="ascii")
    (source / "bundle.js.map").write_text("{}", encoding="ascii")
    with pytest.raises(ValueError, match="source maps"):
        sync_frontend_assets.main()
    assert (target / "index.html").read_bytes() == b"first\nsecond\n"


def test_frontend_wire_types_are_generated_from_backend_contracts() -> None:
    assert (
        generate_api_types.TARGET.read_text(encoding="utf-8")
        == generate_api_types.generated_types()
    )


@pytest.mark.parametrize(
    "name",
    [
        "audio.FLAC",
        "test-clean.tar.gz",
        "release.zip",
        "1-1.trans.txt",
        "inventory.json",
        "selection.json",
        "publication.json",
        "samples.json",
        "provider-config.json",
        "external-manifest.json",
    ],
)
def test_external_payloads_cannot_enter_source_or_deployment_packages(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert external_payload_name(name)
    source = tmp_path / "benchmark_packs" / "example" / name
    source.parent.mkdir(parents=True)
    source.write_bytes(b"invented forbidden fixture")
    monkeypatch.setattr(stage_package, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="External dataset/provider payload"):
        stage_package._validate(source)
    wheel = tmp_path / "invented.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for required in check_wheel.REQUIRED_SUFFIXES:
            archive.writestr(required, b"synthetic")
        archive.writestr(f"benchmark_packs/example/{name}", b"synthetic")
    monkeypatch.setattr(check_wheel, "DIST", tmp_path)
    assert check_wheel.main() == 1
