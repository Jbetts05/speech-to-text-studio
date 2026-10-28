"""FastAPI surface for benchmark execution and evidence retrieval."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_bench.api_admission import HttpAdmission
from ai_bench.api_artifacts import artifact_response
from ai_bench.api_contracts import (
    BenchmarkCatalogEntry,
    CatalogSolution,
    CreateRunResponse,
    FinalizedEvaluationResponse,
    LandingContext,
    LiveDetailRequiredResponse,
    RunDetailResponse,
    RunSummaryResponse,
    RunTaskResponse,
    RuntimeResponse,
)
from ai_bench.api_dataset_adapter import CoreDatasetHttpService, import_description
from ai_bench.api_datasets import register_dataset_routes
from ai_bench.api_policy import ApiPolicy, RequestPolicyMiddleware
from ai_bench.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReadCapacityError,
)
from ai_bench.backends import BackendBundle, BackendNotPromotedError
from ai_bench.contracts import RunManifest, RunReport, utc_now
from ai_bench.costs import CurrentPricingProvider, build_cost_summary, build_finalized_cost_summary
from ai_bench.engine import BenchmarkEngine, NetworkExecutionDisabledError
from ai_bench.finalized import (
    TERMINAL_RUN_STATUSES,
    FinalizedIntegrityError,
    validate_finalized_report,
)
from ai_bench.hashing import normalize_media_type, sha256_digest
from ai_bench.lifecycle import ExpiredRootError, LifecycleRoot, RootUnavailableError
from ai_bench.packs import PackNotFoundError, dataset_availability, discover_packs, get_pack
from ai_bench.reports import report_csv
from ai_bench.repositories import (
    AdmissionCapacityError,
    IdempotencyConflictError,
    LeaseLostError,
    RepositoryError,
)
from ai_bench.runtime_policy import PublicExecutionPolicy
from ai_bench.storage_state import StateConflictError
from ai_bench.worker import RunSupervisor

ARTIFACT_ID = re.compile(r"^[a-f0-9]{64}$")
RUN_ID = re.compile(r"^[a-f0-9]{64}$")
STATIC_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data:; media-src 'self'; object-src 'none'; "
        "script-src 'self'; style-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    benchmark_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    dataset_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{3,96}$")
    solution_ids: list[str] | None = Field(default=None, max_length=20)
    retry_limit: int = Field(default=3, ge=1, le=20)
    allow_networked: bool = False


class RuntimeConfigurationError(RuntimeError):
    pass


def resolve_state_root(explicit: Path | None = None) -> Path:
    runtime = os.getenv("AI_BENCH_RUNTIME", "local").lower()
    if runtime not in {"local", "azure"}:
        raise RuntimeConfigurationError("AI_BENCH_RUNTIME must be either 'local' or 'azure'")
    configured = explicit
    if configured is None:
        environment_root = os.getenv("AI_BENCH_STATE_ROOT")
        configured = Path(environment_root) if environment_root else None
    if runtime == "azure":
        configured = configured or Path("/home/data/ai-bench")
        resolved = configured.resolve()
        persistent_root = Path("/home/data").resolve()
        try:
            resolved.relative_to(persistent_root)
        except ValueError as error:
            raise RuntimeConfigurationError(
                "AI_BENCH_RUNTIME=azure requires an absolute persistent state root "
                "under /home/data (default: /home/data/ai-bench)"
            ) from error
        return resolved
    return configured or Path(".ai-bench")


def create_app(
    state_root: Path | None = None,
    pack_root: Path | None = None,
    readiness_checks: Sequence[Callable[[], bool]] = (),
    *,
    backend: BackendBundle | None = None,
) -> FastAPI:
    policy = ApiPolicy.from_environment()
    resolved_state_root = resolve_state_root(state_root)
    runtime = os.getenv("AI_BENCH_RUNTIME", "local").lower()
    execution_policy = (
        backend.public_policy
        if backend is not None and backend.public_policy is not None
        else PublicExecutionPolicy()
        if policy.public_demo
        else None
    )
    engine = BenchmarkEngine(
        resolved_state_root, pack_root, backend=backend, public_policy=execution_policy
    )
    max_attempts = min(
        policy.max_attempts,
        engine.public_policy.max_attempts
        if engine.public_policy is not None
        else policy.max_attempts,
    )
    supervisor = RunSupervisor(engine)
    admission = HttpAdmission(engine.backend.state, clock=engine.lifecycle.clock)

    def ensure_release_writable() -> None:
        if (
            runtime == "azure"
            and policy.public_demo
            and (not engine.backend.live_proof or engine.landing_context() is None)
        ):
            raise HTTPException(
                status_code=503,
                detail="Hosted backend proof or baseline publication is pending operator setup.",
                headers={"Retry-After": "30"},
            )

    def admit_upload(key: str) -> None:
        if (
            policy.public_demo
            and engine.backend.state.get(f"upload-key:{sha256_digest(key)}") is None
        ):
            admission.admit("upload")

    dataset_service = CoreDatasetHttpService(
        engine,
        ensure_writable=ensure_release_writable,
        admit_upload=admit_upload,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        supervisor.start()
        try:
            yield
        finally:
            await asyncio.to_thread(supervisor.stop)

    configured_checks = list(readiness_checks)
    app = FastAPI(
        title="AI Bench",
        version="0.1.0",
        description=(
            "Evidence-first benchmark API with immutable datasets and resumable uploads. "
            "Local execution uses SQLite/filesystem; hosted execution selects the "
            "conformance-gated Azure Table/Blob backend. One current-generation sender "
            "does not guarantee exactly-once external provider calls."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(RequestPolicyMiddleware, policy=policy)
    app.state.engine = engine
    app.state.supervisor = supervisor
    app.state.dataset_service = dataset_service

    @app.exception_handler(ExpiredRootError)
    async def expired(_request: Request, _error: ExpiredRootError) -> JSONResponse:
        return JSONResponse(status_code=410, content={"detail": "Dataset or run expired."})

    @app.exception_handler(RootUnavailableError)
    async def unavailable(_request: Request, _error: RootUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Published evidence not found."})

    @app.exception_handler(LeaseLostError)
    async def lost_lease(_request: Request, _error: LeaseLostError) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": "Operation ownership changed; refresh its status."}
        )

    @app.exception_handler(StateConflictError)
    async def storage_conflict(_request: Request, _error: StateConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Storage is temporarily busy; retry the same operation."},
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(BackendNotPromotedError)
    async def unpromoted(_request: Request, _error: BackendNotPromotedError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Live backend proof is pending operator validation."},
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(ArtifactReadCapacityError)
    async def artifact_capacity(
        _request: Request, _error: ArtifactReadCapacityError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Artifact reads are temporarily busy; retry the same request."},
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(AdmissionCapacityError)
    async def capacity(_request: Request, error: AdmissionCapacityError) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": "The run queue is full; retry the same operation later."},
            headers={"Retry-After": str(error.retry_after)},
        )

    def live_run(run_id: str) -> LifecycleRoot | None:
        root = engine.assert_run_live(run_id)
        if root is not None and root.state != "ready":
            raise HTTPException(status_code=404, detail="run is not published")
        return root

    def run_dataset_id(run_id: str, root: LifecycleRoot | None) -> str | None:
        if root is None:
            return None
        if root.parent_id is not None:
            return root.parent_id
        if root.archival:
            landing = engine.backend.state.get("landing")
            if landing is not None and landing.values.get("run_id") == run_id:
                identifier = landing.values.get("dataset_id")
                if isinstance(identifier, str):
                    return identifier
                raise RepositoryError("archival dataset identity is invalid")
        return None

    def require_artifact_access(artifact_id: str) -> None:
        managed = engine.backend.state.get(f"object:{artifact_id}") is not None
        if (
            policy.public_demo
            or engine.backend.name != "single-instance-sqlite-filesystem"
            or managed
        ) and not engine.artifact_has_live_owner(artifact_id):
            raise HTTPException(status_code=410, detail="artifact is unpublished or expired")

    def default_dataset_id(benchmark_id: str) -> str | None:
        landing = engine.landing_context()
        if landing is None or landing["benchmark_id"] != benchmark_id:
            return None
        root = engine.lifecycle.get(landing["run_id"])
        identifier = run_dataset_id(landing["run_id"], root)
        if identifier is None:
            raise RepositoryError("installed baseline has no published dataset identity")
        engine.datasets.get(identifier)
        return identifier

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def readiness() -> dict[str, str]:
        try:
            packs = discover_packs(pack_root)
            if not packs:
                raise RuntimeError("no benchmark packs are available")
            engine.repository.list_runs(limit=1)
            engine.backend.assert_ready()
            if (
                runtime == "azure"
                and policy.public_demo
                and (not engine.backend.live_proof or engine.landing_context() is None)
            ):
                raise RuntimeError("hosted backend proof or baseline publication is pending")
            if engine.lifecycle.health().error is not None:
                raise RuntimeError("retention cleanup is unavailable")
            if not all(check() for check in configured_checks):
                raise RuntimeError("a configured dependency is not ready")
            if supervisor.error is not None:
                raise RuntimeError(supervisor.error)
        except Exception as error:
            raise HTTPException(
                status_code=503, detail="repository or packs unavailable"
            ) from error
        return {"status": "ready"}

    @app.get("/api/runtime")
    def runtime_status() -> RuntimeResponse:
        return RuntimeResponse(
            runtime=runtime,
            execution_backend=engine.backend.name,
            horizontal_scaling=False,
            persistent_state_required=runtime == "azure",
            azure_storage_required=engine.backend.name == "azure-table-blob",
            azure_storage_role=(
                "active-immutable-objects-and-table-coordination"
                if engine.backend.name == "azure-table-blob"
                else "not-required-by-local-backend"
            ),
            worker_running=supervisor.running,
            worker_error=(
                None if supervisor.error is None else "Worker unavailable; inspect operator logs."
            ),
            public_demo=policy.public_demo,
            allow_networked=(
                os.getenv("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS", "false").lower() == "true"
            ),
            max_attempts=max_attempts,
            retention_days=10 if policy.public_demo else None,
            server_time=utc_now(),
            sender_policy="current-generation",
            external_call_duplicate_risk=True,
            landing=(
                None
                if (landing := engine.landing_context()) is None
                else LandingContext.model_validate(landing)
            ),
        )

    @app.get("/api/benchmarks")
    @app.get("/api/packs")
    def benchmarks() -> list[BenchmarkCatalogEntry]:
        payloads: list[BenchmarkCatalogEntry] = []
        for discovered in discover_packs(pack_root).values():
            availability = dataset_availability(discovered.pack)
            selected_dataset = default_dataset_id(discovered.pack.definition.id)
            if selected_dataset is not None:
                availability = engine.datasets.resolve(
                    selected_dataset, discovered.pack.definition.id
                ).availability()
            payloads.append(
                BenchmarkCatalogEntry(
                    config=discovered.config,
                    definition=discovered.pack.definition,
                    solutions=tuple(
                        CatalogSolution(
                            id=solution.id,
                            display_name=getattr(solution, "display_name", None),
                            networked=solution.networked,
                            pipeline=solution.pipeline,
                            compatibility=solution.compatibility,
                        )
                        for solution in discovered.pack.solutions
                    ),
                    metrics=tuple(discovered.pack.evaluator.metric_definitions),
                    sample_count=availability.sample_count,
                    scored_sample_count=availability.scored_sample_count,
                    dataset_availability=availability,
                    viewer=discovered.pack.viewer.descriptor,
                    dataset_import=import_description(engine, discovered.pack.definition.id),
                    default_dataset_id=selected_dataset,
                )
            )
        return payloads

    @app.post("/api/runs", status_code=202)
    def create_run(
        request: CreateRunRequest,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=200,
                pattern=r"^[\x21-\x7e]+$",
            ),
        ] = None,
    ) -> CreateRunResponse:
        ensure_release_writable()
        if request.retry_limit > max_attempts:
            raise HTTPException(
                status_code=422,
                detail=f"This runtime permits at most {max_attempts} total attempts.",
            )
        if policy.public_demo:
            known_key = (
                idempotency_key is not None
                and engine.repository.get_launch_reservation(
                    sha256_digest({"idempotency_key": idempotency_key})
                )
                is not None
            )
            policy.validate_key(idempotency_key, known=known_key)
            if not known_key:
                admission.admit("run")
        try:
            selected_dataset = request.dataset_id
            if selected_dataset is None and (runtime == "azure" or policy.public_demo):
                selected_dataset = default_dataset_id(request.benchmark_id)
            manifest = engine.create_run(
                request.benchmark_id,
                solution_ids=(
                    None if request.solution_ids is None else tuple(request.solution_ids)
                ),
                retry_limit=request.retry_limit,
                allow_networked=request.allow_networked,
                idempotency_key=idempotency_key,
                dataset_id=selected_dataset,
            )
            persisted_run = engine.repository.get_run(manifest.run_id)
            if persisted_run is None:
                raise RepositoryError("run initialization did not persist its manifest")
            supervisor.wake()
        except PackNotFoundError as error:
            raise HTTPException(status_code=404, detail="benchmark pack not found") from error
        except IdempotencyConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ValueError, NetworkExecutionDisabledError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return CreateRunResponse(
            run_id=manifest.run_id,
            status=persisted_run.status,
            manifest_ref=f"/api/artifacts/{persisted_run.manifest_artifact_id}",
            manifest_artifact_id=persisted_run.manifest_artifact_id,
            report_ref=(
                None
                if persisted_run.report_artifact_id is None
                else f"/api/artifacts/{persisted_run.report_artifact_id}"
            ),
            report_artifact_id=persisted_run.report_artifact_id,
        )

    @app.get("/api/runs")
    def runs(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[RunSummaryResponse]:
        summaries: list[RunSummaryResponse] = []
        for run in engine.repository.list_runs(limit):
            try:
                root = live_run(run.manifest.run_id)
            except (ExpiredRootError, RootUnavailableError, HTTPException):
                continue
            summaries.append(
                RunSummaryResponse(
                    run_id=run.manifest.run_id,
                    benchmark_id=run.manifest.benchmark.id,
                    created_at=run.manifest.created_at,
                    status=run.status,
                    runtime_error=run.runtime_error,
                    manifest_artifact_id=run.manifest_artifact_id,
                    report_artifact_id=run.report_artifact_id,
                    dataset_id=run_dataset_id(run.manifest.run_id, root),
                    expires_at=None if root is None else root.expires_at,
                    pinned=root is not None and root.pinned,
                    archival=root is not None and root.archival,
                )
            )
        return summaries

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> RunDetailResponse:
        _validate_identifier(run_id, RUN_ID, "run")
        root = live_run(run_id)
        run = engine.repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        tasks = tuple(engine.repository.list_tasks(run_id))
        histories = {task.id: tuple(engine.repository.list_attempts(task.id)) for task in tasks}
        try:
            pack = get_pack(run.manifest.benchmark.id, pack_root)
        except PackNotFoundError:
            pricing_provider = None
        else:
            pricing_provider = getattr(pack, "pricing_provider", None)
            if pricing_provider is not None and not isinstance(
                pricing_provider, CurrentPricingProvider
            ):
                raise TypeError("pack pricing_provider must implement the current pricing contract")
        report = engine.get_report(run_id) if run.report_artifact_id else None
        return RunDetailResponse(
            costs=build_cost_summary(
                run.manifest,
                run.manifest_artifact_id,
                tasks,
                histories,
                pricing_provider,
                report,
            ),
            manifest=run.manifest,
            manifest_ref=f"/api/artifacts/{run.manifest_artifact_id}",
            manifest_artifact_id=run.manifest_artifact_id,
            status=run.status,
            runtime_error=run.runtime_error,
            dataset_id=run_dataset_id(run_id, root),
            expires_at=None if root is None else root.expires_at,
            pinned=root is not None and root.pinned,
            archival=root is not None and root.archival,
            sample_artifacts=engine.sample_evidence(run.manifest),
            report_ref=(
                None
                if run.report_artifact_id is None
                else f"/api/artifacts/{run.report_artifact_id}"
            ),
            report_artifact_id=run.report_artifact_id,
            tasks=tuple(
                RunTaskResponse(
                    **task.model_dump(),
                    attempt_history=histories[task.id],
                )
                for task in tasks
            ),
        )

    @app.get("/api/runs/{run_id}/events")
    def run_events(
        run_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    ) -> list[dict[str, object]]:
        _validate_identifier(run_id, RUN_ID, "run")
        live_run(run_id)
        if engine.repository.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return [
            event.model_dump(mode="json")
            for event in engine.repository.list_events(run_id, after, limit)
        ]

    @app.get("/api/runs/{run_id}/finalized-evaluation")
    def finalized_evaluation(
        run_id: str, response: Response
    ) -> FinalizedEvaluationResponse | LiveDetailRequiredResponse:
        _validate_identifier(run_id, RUN_ID, "run")
        response.headers["Cache-Control"] = "no-store"
        root = live_run(run_id)
        run = engine.repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        dataset_id = run_dataset_id(run_id, root)
        if dataset_id is not None:
            engine.lifecycle.require_live(dataset_id)

        def summary() -> RunSummaryResponse:
            current_root = live_run(run_id)
            if current_root != root:
                raise HTTPException(status_code=409, detail="Run lifecycle changed; retry.")
            if dataset_id is not None:
                engine.lifecycle.require_live(dataset_id)
            return RunSummaryResponse(
                run_id=run_id,
                benchmark_id=run.manifest.benchmark.id,
                created_at=run.manifest.created_at,
                status=run.status,
                runtime_error=run.runtime_error,
                manifest_artifact_id=run.manifest_artifact_id,
                report_artifact_id=run.report_artifact_id,
                dataset_id=dataset_id,
                expires_at=None if root is None else root.expires_at,
                pinned=root is not None and root.pinned,
                archival=root is not None and root.archival,
            )

        if run.status not in TERMINAL_RUN_STATUSES or run.report_artifact_id is None:
            return LiveDetailRequiredResponse(
                record_source="live_detail_required",
                run=summary(),
                reason="run_not_finalized"
                if run.status not in TERMINAL_RUN_STATUSES
                else "report_missing",
                detail="Finalized evaluation evidence is not available; load live run details.",
            )
        require_artifact_access(run.manifest_artifact_id)
        require_artifact_access(run.report_artifact_id)
        try:
            manifest = RunManifest.model_validate_json(
                engine.artifacts.get_bytes(run.manifest_artifact_id), strict=False
            )
            report = RunReport.model_validate_json(
                engine.artifacts.get_bytes(run.report_artifact_id), strict=False
            )
            unavailable_reason = validate_finalized_report(run_id, run, manifest, report)
        except (
            ArtifactIntegrityError,
            ArtifactNotFoundError,
            ValidationError,
            FinalizedIntegrityError,
        ) as error:
            raise HTTPException(
                status_code=409, detail="Finalized evaluation evidence failed integrity validation."
            ) from error
        if unavailable_reason is not None:
            return LiveDetailRequiredResponse(
                record_source="live_detail_required",
                run=summary(),
                reason=unavailable_reason,
                detail="The immutable report lacks complete historical records; load live details.",
            )
        try:
            pack = get_pack(manifest.benchmark.id, pack_root)
        except PackNotFoundError:
            pricing_provider = None
        else:
            pricing_provider = getattr(pack, "pricing_provider", None)
            if pricing_provider is not None and not isinstance(
                pricing_provider, CurrentPricingProvider
            ):
                raise TypeError("pack pricing_provider must implement the current pricing contract")
        costs = build_finalized_cost_summary(manifest, report, pricing_provider)
        sample_artifacts = engine.sample_evidence(manifest)
        require_artifact_access(run.manifest_artifact_id)
        require_artifact_access(run.report_artifact_id)
        return FinalizedEvaluationResponse(
            record_source="finalized_report",
            run=summary(),
            manifest=manifest,
            report=report,
            costs=costs,
            sample_artifacts=sample_artifacts,
        )

    @app.get("/api/runs/{run_id}/report")
    def run_report(
        run_id: str,
        format: Literal["json", "csv"] = "json",
    ) -> Response:
        _validate_identifier(run_id, RUN_ID, "run")
        root = live_run(run_id)
        report = engine.get_report(run_id)
        if report is None:
            if engine.repository.get_run(run_id) is None:
                raise HTTPException(status_code=404, detail="run not found")
            raise HTTPException(status_code=409, detail="run report is not finalized")
        if format == "csv":
            if root is not None and root.archival:
                _, csv_path = engine.export_report(run_id)
                return FileResponse(csv_path, media_type="text/csv", filename=f"{run_id}.csv")
            return PlainTextResponse(
                report_csv(report),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{run_id}.csv"',
                },
            )
        return Response(engine.get_report_bytes(run_id), media_type="application/json")

    @app.api_route("/api/artifacts/{artifact_id}", methods=["GET", "HEAD"])
    def artifact(
        request: Request,
        artifact_id: str,
        accept: Annotated[str | None, Header(alias="Accept")] = None,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
        if_range: Annotated[str | None, Header(alias="If-Range")] = None,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
        download: bool = False,
    ) -> Response:
        _validate_identifier(artifact_id, ARTIFACT_ID, "artifact")
        require_artifact_access(artifact_id)
        registered = None
        for media_type in _accepted_media_types(accept):
            try:
                registered = engine.artifacts.get(artifact_id, media_type=media_type)
                break
            except ArtifactNotFoundError:
                continue
        try:
            registered = registered or engine.artifacts.get(artifact_id)
        except ArtifactNotFoundError as error:
            raise HTTPException(status_code=404, detail="artifact not found") from error
        try:
            with ExitStack() as resources:
                stream = resources.enter_context(engine.artifacts.open_verified(artifact_id))
                require_artifact_access(artifact_id)
                owner = resources.pop_all()
                return artifact_response(
                    stream,
                    artifact_id=artifact_id,
                    size_bytes=registered.size_bytes,
                    media_type=registered.media_type,
                    method=request.method,
                    range_header=range_header,
                    if_range=if_range,
                    if_none_match=if_none_match,
                    download=download,
                    on_close=owner.close,
                )
        except (ArtifactNotFoundError, ArtifactIntegrityError) as error:
            raise HTTPException(
                status_code=409, detail="artifact content is not available in this repository"
            ) from error

    register_dataset_routes(app, dataset_service, policy=policy, wake_worker=supervisor.wake)

    static_root = Path(__file__).resolve().parent / "web" / "static"
    index = static_root / "index.html"
    if index.is_file():

        @app.get("/{path:path}", include_in_schema=False)
        def static_fallback(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API route not found")
            requested = (static_root / path).resolve()
            if path and static_root.resolve() in requested.parents and requested.is_file():
                return FileResponse(requested, headers=STATIC_SECURITY_HEADERS)
            return FileResponse(index, headers=STATIC_SECURITY_HEADERS)

    return app


def _validate_identifier(value: str, pattern: re.Pattern[str], kind: str) -> None:
    if not pattern.fullmatch(value):
        raise HTTPException(status_code=404, detail=f"{kind} not found")


def _accepted_media_types(header: str | None) -> tuple[str, ...]:
    if header is None:
        return ()
    accepted: list[str] = []
    for item in header.split(","):
        parts = [part.strip() for part in item.split(";")]
        media_type = parts[0].casefold()
        if not media_type or "*" in media_type:
            continue
        parameters = [part for part in parts[1:] if part and not part.casefold().startswith("q=")]
        accepted.append(normalize_media_type(";".join((media_type, *parameters))))
    return tuple(accepted)
