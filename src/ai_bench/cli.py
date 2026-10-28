"""Command-line interface for AI Bench."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ai_bench.api import create_app, resolve_state_root
from ai_bench.archives import export_archive, import_archive, validate_archive
from ai_bench.backends import azure_backend, local_backend
from ai_bench.conformance import run_conformance
from ai_bench.engine import BenchmarkEngine
from ai_bench.packs import PackValidationError, discover_packs, validate_pack
from ai_bench.scaffold import BriefValidationError, scaffold_pack, validate_brief

app = typer.Typer(no_args_is_help=True, help="Run generic evidence-first AI benchmarks.")
archive_app = typer.Typer(no_args_is_help=True, help="Operator-only exact-byte terminal archives.")
storage_app = typer.Typer(no_args_is_help=True, help="Operator-only storage conformance.")
app.add_typer(archive_app, name="archive")
app.add_typer(storage_app, name="storage")
DEFAULT_STATE_ROOT = resolve_state_root()


@app.command()
def validate(
    brief: Annotated[
        Path | None,
        typer.Option("--brief", help="Validate a BENCHMARK.md-compatible brief."),
    ] = None,
    pack_root: Annotated[
        Path | None, typer.Option("--pack-root", help="Directory containing benchmark packs.")
    ] = None,
) -> None:
    """Validate the benchmark brief and all discoverable packs."""
    try:
        if brief is not None:
            validate_brief(brief)
        packs = discover_packs(pack_root)
        for discovered in packs.values():
            validate_pack(discovered.pack, discovered.config)
    except (BriefValidationError, PackValidationError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Validated {len(packs)} benchmark pack(s).")


@app.command()
def scaffold(
    slug: Annotated[str, typer.Argument(help="New pack identifier.")],
    brief: Annotated[Path | None, typer.Option("--brief")] = None,
    pack_root: Annotated[Path, typer.Option("--pack-root")] = Path("benchmark_packs"),
) -> None:
    """Create a new pack skeleton without overwriting an existing pack."""
    try:
        destination = scaffold_pack(slug, pack_root, brief)
    except (BriefValidationError, FileExistsError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(str(destination))


@app.command("run")
def run_command(
    pack_id: Annotated[str, typer.Argument(help="Benchmark pack identifier.")] = (
        "synthetic_text_intent"
    ),
    solution: Annotated[
        list[str] | None,
        typer.Option("--solution", help="Solution identifier; repeat to select several."),
    ] = None,
    retry_limit: Annotated[int, typer.Option(min=1, max=20)] = 3,
    state_root: Annotated[Path, typer.Option("--state-root")] = DEFAULT_STATE_ROOT,
    pack_root: Annotated[Path | None, typer.Option("--pack-root")] = None,
) -> None:
    """Execute a benchmark locally; networked providers remain disabled."""
    engine = BenchmarkEngine(state_root, pack_root)
    manifest = engine.create_run(
        pack_id,
        solution_ids=None if solution is None else tuple(solution),
        retry_limit=retry_limit,
    )
    report = engine.run_to_completion(manifest.run_id)
    json_path, csv_path = engine.export_report(manifest.run_id)
    typer.echo(
        json.dumps(
            {
                "run_id": manifest.run_id,
                "status": report.status.value,
                "json_report": str(json_path),
                "csv_report": str(csv_path),
            },
            sort_keys=True,
        )
    )


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="Run identifier.")],
    output_format: Annotated[str, typer.Option("--format", help="json or csv")] = "json",
    state_root: Annotated[Path, typer.Option("--state-root")] = DEFAULT_STATE_ROOT,
) -> None:
    """Print a finalized immutable report."""
    if output_format not in {"json", "csv"}:
        raise typer.BadParameter("format must be json or csv")
    engine = BenchmarkEngine(state_root)
    report_value = engine.get_report(run_id)
    if report_value is None:
        raise typer.BadParameter("run does not exist or is not finalized")
    if output_format == "json":
        typer.echo(report_value.model_dump_json(indent=2))
    else:
        from ai_bench.reports import report_csv

        typer.echo(report_csv(report_value), nl=False)


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    state_root: Annotated[Path, typer.Option("--state-root")] = DEFAULT_STATE_ROOT,
    pack_root: Annotated[Path | None, typer.Option("--pack-root")] = None,
) -> None:
    """Serve the API. The default bind is loopback-only."""
    import uvicorn

    uvicorn.run(create_app(state_root, pack_root), host=host, port=port)


@archive_app.command("export")
def archive_export(
    run_id: str,
    destination: Annotated[Path, typer.Option("--destination")],
    state_root: Annotated[Path, typer.Option("--state-root")] = DEFAULT_STATE_ROOT,
    pack_root: Annotated[Path | None, typer.Option("--pack-root")] = None,
) -> None:
    """Export a terminal evidence graph, never rerunning providers."""
    digest = export_archive(BenchmarkEngine(state_root, pack_root), run_id, destination)
    typer.echo(json.dumps({"archive_digest": digest}, sort_keys=True))


@archive_app.command("validate")
def archive_validate(
    bundle: Path,
    expected_digest: Annotated[str | None, typer.Option("--expected-digest")] = None,
) -> None:
    """Verify bounded object bytes, descriptors and the complete terminal graph."""
    index = validate_archive(bundle, expected_digest=expected_digest)
    typer.echo(
        json.dumps(
            {
                "run_id": index.run.manifest.run_id,
                "objects": len(index.objects),
                "tasks": len(index.tasks),
                "validated": True,
            },
            sort_keys=True,
        )
    )


@archive_app.command("import")
def archive_import(
    bundle: Path,
    expected_digest: Annotated[str, typer.Option("--expected-digest")],
    state_root: Annotated[Path, typer.Option("--state-root")] = DEFAULT_STATE_ROOT,
    local: Annotated[bool, typer.Option("--local-durable")] = False,
) -> None:
    """Import an operator-reviewed baseline and atomically pin its two roots."""
    backend = local_backend(state_root, durable_projections=True) if local else None
    engine = BenchmarkEngine(state_root, backend=backend)
    run_id = import_archive(engine, bundle, expected_digest=expected_digest)
    typer.echo(json.dumps({"run_id": run_id, "archival": True}, sort_keys=True))


@storage_app.command("conformance")
def storage_conformance(
    benchmark_id: Annotated[str, typer.Option("--benchmark")],
    solution_id: Annotated[str, typer.Option("--solution")],
    state_root: Annotated[Path, typer.Option("--state-root")],
    pack_root: Annotated[Path | None, typer.Option("--pack-root")] = None,
    local: Annotated[bool, typer.Option("--local-durable")] = False,
) -> None:
    """Exercise synthetic storage scenarios in an empty disposable namespace."""
    import os

    if (
        not local
        and os.getenv("AI_BENCH_STORAGE_BACKEND", "local").startswith("azure")
        and not os.getenv("AI_BENCH_AZURE_NAMESPACE", "").startswith("conformance-")
    ):
        raise typer.BadParameter(
            "live conformance requires an operation-owned conformance- namespace"
        )
    backend = local_backend(state_root, durable_projections=True) if local else None
    if not local and os.getenv("AI_BENCH_STORAGE_BACKEND", "local").startswith("azure"):
        backend = azure_backend(state_root / "cache", verify_existing_proof=False)
    result = run_conformance(
        BenchmarkEngine(state_root, pack_root, backend=backend),
        benchmark_id=benchmark_id,
        solution_id=solution_id,
    )
    typer.echo(result.model_dump_json())


if __name__ == "__main__":
    app()
