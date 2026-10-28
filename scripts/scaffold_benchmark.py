"""Compatibility wrapper for the ai-bench scaffold command."""

from pathlib import Path

import typer

from ai_bench.scaffold import scaffold_pack


def main(slug: str, root: Path = Path("benchmark_packs"), brief: Path | None = None) -> None:
    destination = scaffold_pack(slug, root, brief)
    typer.echo(str(destination))


if __name__ == "__main__":
    typer.run(main)
