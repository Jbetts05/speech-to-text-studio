"""Operator-only runtime checks; never starts an application server or provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.backend_proof import (
    QualificationRequiredError,
    backend_code_signature,
    verify_receipt,
)
from ai_bench.backends import azure_backend
from ai_bench.conformance import run_conformance
from ai_bench.engine import BenchmarkEngine
from ai_bench.storage_state import AzureTableStateStore
from infra.scripts.runtime_probe import run_probe, worker_identity

DEPENDENCIES = (
    "azure-core",
    "azure-data-tables",
    "azure-storage-blob",
    "azure-identity",
    "pydantic",
)


def inspect_package(root: Path) -> dict[str, str]:
    manifest = json.loads((root / "package-manifest.json").read_bytes())
    files: dict[str, str] = {}
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or item["path"] in files:
            raise ValueError("invalid package manifest path")
        data = (root / relative).read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError(f"serving/candidate source differs from package: {relative}")
        files[item["path"]] = item["sha256"]
    if (root / "requirements.txt").read_bytes() != (root / "requirements.lock").read_bytes():
        raise ValueError("runtime requirements differ from locked package")
    return files


def examine(
    root: Path, state_root: Path, worker_pid: int, *, digest: str | None = None
) -> dict[str, Any]:
    import ai_bench

    worker = worker_identity(worker_pid)
    if Path(ai_bench.__file__).resolve() != root / "src" / "ai_bench" / "__init__.py":
        raise ValueError("operator imported a different application package")
    if os.environ.get("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS") != "false":
        raise ValueError("operator process must disable networked execution")
    files = inspect_package(root)
    backend = azure_backend(state_root / "cache", verify_existing_proof=False)
    if not isinstance(backend.state, AzureTableStateStore) or not isinstance(
        backend.artifacts, AzureBlobArtifactStore
    ):
        raise ValueError("release qualification requires the real Table transport")
    checkpoint = digest or os.environ.get("AI_BENCH_BACKEND_CONFORMANCE_RECEIPT", "")
    needs_qualification = not checkpoint
    if checkpoint:
        try:
            needs_qualification = not verify_receipt(
                backend.state, backend.artifacts, checkpoint, backend.configuration_signature
            )
        except QualificationRequiredError:
            needs_qualification = True
    engine = BenchmarkEngine(state_root, root / "benchmark_packs", backend=backend)
    return {
        "worker": worker,
        "python": list(sys.version_info[:2]),
        "executable": sys.executable,
        "versions": {name: version(name) for name in DEPENDENCIES},
        "sys_path": sys.path,
        "module": ai_bench.__file__,
        "files": files,
        "code_signature": backend_code_signature(),
        "configuration_signature": backend.configuration_signature,
        "qualification_required": needs_qualification,
        "active_runs": [run.manifest.run_id for run in engine.repository.unfinished_runs()],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "qualify", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--namespace")
    parser.add_argument("--digest")
    args = parser.parse_args()
    root = args.root.resolve()
    proof = examine(root, args.state_root, args.worker_pid, digest=args.digest)
    if proof["active_runs"]:
        raise ValueError("active user work prevents release")
    if args.action == "inspect":
        proof["connectivity"] = run_probe(
            args.worker_pid,
            ["AI_BENCH_SPEECH_ENDPOINT", "AI_BENCH_OPENAI_ENDPOINT", "AI_BENCH_MAI_ENDPOINT"],
        )
    elif args.action == "qualify":
        if not args.namespace or not args.namespace.startswith("conformance-"):
            raise ValueError("explicit operation-owned conformance namespace required")
        if args.namespace == os.environ["AI_BENCH_AZURE_NAMESPACE"]:
            raise ValueError("conformance must never use the application namespace")
        os.environ["AI_BENCH_AZURE_NAMESPACE"] = args.namespace
        backend = azure_backend(
            args.state_root / "qualification-cache", verify_existing_proof=False
        )
        if not isinstance(backend.state, AzureTableStateStore) or not isinstance(
            backend.artifacts, AzureBlobArtifactStore
        ):
            raise ValueError("qualification requires real Table/Blob transports")
        if backend.state.page("", limit=1).rows:
            raise ValueError("conformance namespace is not empty; do not retry or clear it")
        for store in (backend.artifacts, backend.staging):
            if not isinstance(store, AzureBlobArtifactStore):
                raise ValueError("missing qualification object store")
            if (
                next(
                    iter(
                        store.container.list_blobs(
                            name_starts_with=f"namespaces/{args.namespace}/", results_per_page=1
                        )
                    ),
                    None,
                )
                is not None
            ):
                raise ValueError("conformance object prefix is not empty")
        engine = BenchmarkEngine(
            args.state_root / "qualification", root / "benchmark_packs", backend=backend
        )
        result = run_conformance(
            engine, benchmark_id="synthetic_text_intent", solution_id="keyword-standalone"
        )
        if not result.live_promotion or result.provider_calls or not result.receipt_digest:
            raise ValueError("real zero-inference qualification did not publish proof")
        if (
            inspect_package(root) != proof["files"]
            or backend_code_signature() != proof["code_signature"]
        ):
            raise ValueError("candidate source changed during qualification")
        if not verify_receipt(
            backend.state,
            backend.artifacts,
            result.receipt_digest,
            backend.configuration_signature,
            require_current_build=True,
        ):
            raise ValueError("published qualification does not match exact candidate")
        proof["qualification"] = result.model_dump(mode="json")
    elif proof["qualification_required"]:
        raise ValueError("serving backend qualification is missing or inadequate")
    print(json.dumps(proof, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
