from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from infra.scripts._azure import CommandError, az_json, azd_value, require_command
from infra.scripts.hosted_checks import verify_arm

ARTIFACT_REF = re.compile(r"^/api/artifacts/([a-f0-9]{64})$")


def _subscription_from_scope(scope: str) -> str:
    matched = re.match(r"^/subscriptions/([^/]+)(?:/|$)", scope, flags=re.IGNORECASE)
    if matched is None:
        raise CommandError(f"Cannot determine subscription from Azure scope: {scope}")
    return matched.group(1)


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
    timeout: float = 20,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    headers.update(extra_headers or {})
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise CommandError(f"{method} {url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CommandError(f"{method} {url} failed: {exc}") from exc
    if not body:
        return {}
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CommandError(f"{method} {url} returned invalid JSON") from exc
    return decoded


def _request_object(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    extra_headers: Mapping[str, str] | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    decoded = _request_json(
        url,
        method=method,
        payload=payload,
        token=token,
        extra_headers=extra_headers,
        timeout=timeout,
    )
    if not isinstance(decoded, dict):
        raise CommandError(f"{method} {url} did not return a JSON object")
    return decoded


def _request_array(url: str) -> list[dict[str, Any]]:
    decoded = _request_json(url)
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise CommandError(f"GET {url} did not return a JSON object array")
    return decoded


def _request_bytes(url: str, *, timeout: float = 20) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        raise CommandError(f"GET {url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CommandError(f"GET {url} failed: {exc}") from exc


def _verified_artifact_bytes(app_url: str, reference: str) -> bytes:
    matched = ARTIFACT_REF.fullmatch(reference)
    if matched is None:
        raise CommandError(f"Invalid immutable artifact reference: {reference}")
    content = _request_bytes(f"{app_url}{reference}")
    if hashlib.sha256(content).hexdigest() != matched.group(1):
        raise CommandError(f"Artifact content does not match its SHA-256 identity: {reference}")
    return content


def _verified_artifact_object(app_url: str, reference: str) -> dict[str, Any]:
    content = _verified_artifact_bytes(app_url, reference)
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CommandError(f"Artifact does not contain valid JSON: {reference}") from exc
    if not isinstance(decoded, dict):
        raise CommandError(f"Artifact does not contain a JSON object: {reference}")
    return decoded


def _verify_audio_delivery(app_url: str, evidence: dict[str, Any]) -> None:
    reference = f"/api/artifacts/{evidence['artifact_id']}"
    data = _verified_artifact_bytes(app_url, reference)
    if not data or evidence.get("size_bytes") != len(data):
        raise CommandError("Audio descriptor length does not match immutable bytes.")
    media_type = evidence.get("media_type")
    if media_type not in {"audio/flac", "audio/wav", "audio/mpeg"}:
        raise CommandError("Proof input is not an allowed audio media type.")

    def request(method: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        query = urllib.request.Request(f"{app_url}{reference}", headers=headers, method=method)
        try:
            response = urllib.request.urlopen(query, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code != 416:
                raise CommandError(f"Audio delivery returned HTTP {error.code}.") from error
            response = error
        with response:
            return response.status, dict(response.headers), response.read()

    status, headers, body = request("HEAD", {})
    head = {name.casefold(): value for name, value in headers.items()}
    if (
        status != 200
        or body
        or head.get("content-length") != str(len(data))
        or head.get("content-type", "").split(";")[0] != media_type
        or head.get("accept-ranges") != "bytes"
        or "no-store" not in head.get("cache-control", "")
        or not head.get("etag")
    ):
        raise CommandError("Audio HEAD/content-type/length/cache/range metadata is incorrect.")
    start = len(data) // 2
    status, headers, body = request(
        "GET", {"Range": f"bytes={start}-{start}", "If-Range": head["etag"]}
    )
    partial = {name.casefold(): value for name, value in headers.items()}
    if (
        status != 206
        or body != data[start : start + 1]
        or partial.get("content-range") != f"bytes {start}-{start}/{len(data)}"
        or partial.get("content-length") != "1"
    ):
        raise CommandError("Audio Range/If-Range did not preserve the requested original bytes.")
    status, headers, _ = request("GET", {"Range": f"bytes={len(data)}-"})
    invalid = {name.casefold(): value for name, value in headers.items()}
    if status != 416 or invalid.get("content-range") != f"bytes */{len(data)}":
        raise CommandError("Unsatisfiable audio Range must return correct HTTP 416 metadata.")


def _wait_endpoint(url: str, expected_status: str | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 180
    last_error: CommandError | None = None
    while time.monotonic() < deadline:
        try:
            payload = _request_object(url)
            if expected_status is None or payload.get("status") == expected_status:
                return payload
            last_error = CommandError(
                f"{url} returned status {payload.get('status')!r}, expected {expected_status!r}"
            )
        except CommandError as exc:
            last_error = exc
        time.sleep(5)
    raise CommandError(f"Endpoint did not become ready: {last_error}")


def _verify_kudu(app_name: str, subscription_id: str) -> None:
    token_payload = az_json(
        [
            "account",
            "get-access-token",
            "--resource",
            "https://management.azure.com/",
            "--subscription",
            subscription_id,
        ]
    )
    token = str(token_payload["accessToken"])
    url = f"https://{app_name}.scm.azurewebsites.net/api/settings"
    _request_object(url, token=token)


def _verify_arm_state(
    subscription_id: str,
    resource_group: str,
    app_name: str,
    storage_name: str,
    vault_name: str,
    auth_mode: str,
    foundry_scope: str,
    *,
    mai_scope: str = "",
    public_demo: bool = False,
    scm_cidr: str = "",
    expected_endpoint_ids: tuple[str, ...] = (),
) -> str:
    return verify_arm(
        az_json,
        subscription_id,
        resource_group,
        app_name,
        storage_name,
        vault_name,
        auth_mode,
        tuple(scope for scope in (foundry_scope, mai_scope) if scope),
        public_demo=public_demo,
        scm_cidr=scm_cidr,
        expected_endpoint_ids=expected_endpoint_ids,
    )


def _proof_key(path: Path, payload: dict[str, Any], new_key: str) -> str:
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if path.is_symlink():
        raise CommandError("Proof state cannot be a symlink.")
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if (
            state.get("schema_version") != "provider-proof-v1"
            or state.get("fingerprint") != fingerprint
            or not re.fullmatch(r"v1\.[0-9]{10,12}\.[a-f0-9-]{36}", str(state.get("key", "")))
        ):
            raise CommandError("Proof state does not match this exact dataset/solution request.")
        return str(state["key"])
    with path.open("x", encoding="utf-8") as stream:
        json.dump(
            {"schema_version": "provider-proof-v1", "fingerprint": fingerprint, "key": new_key},
            stream,
            sort_keys=True,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return new_key


def _proof_run(
    app_url: str,
    benchmark: str,
    solution: str | list[str],
    *,
    dataset_id: str | None = None,
    proof_state: Path | None = None,
) -> None:
    solutions = [solution] if isinstance(solution, str) else solution
    if not solutions or len(set(solutions)) != len(solutions):
        raise CommandError("Proof solutions must be nonempty and distinct.")
    packs = _request_array(f"{app_url}/api/benchmarks")
    selected_pack = next(
        (
            pack
            for pack in packs
            if isinstance(pack.get("definition"), dict)
            and pack["definition"].get("id") == benchmark
        ),
        None,
    )
    if selected_pack is None:
        raise CommandError(f"Proof benchmark is not deployed: {benchmark}")
    available_solutions = selected_pack.get("solutions")
    if not isinstance(available_solutions, list):
        raise CommandError(f"Proof benchmark has an invalid solution catalog: {benchmark}")
    for identifier in solutions:
        selected_solution = next(
            (
                candidate
                for candidate in available_solutions
                if isinstance(candidate, dict) and candidate.get("id") == identifier
            ),
            None,
        )
        if selected_solution is None:
            raise CommandError(
                f"Proof solution {identifier} is not available in benchmark {benchmark}."
            )
        if selected_solution.get("networked") is not True:
            raise CommandError(
                "Deployment proof requires a solution declared as networked so an offline "
                "fixture cannot be mistaken for a real provider call."
            )
    payload: dict[str, Any] = {
        "benchmark_id": benchmark,
        "solution_ids": solutions,
        "retry_limit": 3,
        "allow_networked": True,
    }
    headers = {"Idempotency-Key": f"deployment-proof-{uuid4().hex}"}
    dataset: dict[str, Any] = {}
    if dataset_id:
        if len(solutions) != 5:
            raise CommandError("Public hosted proof requires exactly five selected solutions.")
        dataset = _request_object(f"{app_url}/api/datasets/{dataset_id}")
        if (
            dataset.get("sample_count") != 2
            or dataset.get("scored_sample_count") != 2
            or dataset.get("id") != dataset_id
            or dataset.get("benchmark_id") != benchmark
            or dataset.get("pinned") is not False
        ):
            raise CommandError("Proof requires a published, unpinned, two-pair synthetic dataset.")
        try:
            published = datetime.fromisoformat(str(dataset["published_at"]))
            expires = datetime.fromisoformat(str(dataset["expires_at"]))
            runtime = _request_object(f"{app_url}/api/runtime")
            server_time = datetime.fromisoformat(str(runtime["server_time"]))
        except (ValueError, KeyError) as error:
            raise CommandError(
                "Proof requires server-time and ten-day publication metadata."
            ) from error
        if (
            any(value.tzinfo is None for value in (published, expires, server_time))
            or expires - published != timedelta(days=10)
            or server_time >= expires
        ):
            raise CommandError("Synthetic publication does not have a live fixed ten-day deadline.")
        payload["dataset_id"] = dataset_id
        new_key = f"v1.{int(server_time.timestamp())}.{uuid4()}"
        key = (
            _proof_key(proof_state, {"app_url": app_url, "request": payload}, new_key)
            if proof_state
            else new_key
        )
        headers = {
            "Idempotency-Key": key,
            "X-AI-Bench-Intent": "public-demo",
            "Origin": app_url,
        }

    created = _request_object(
        f"{app_url}/api/runs",
        method="POST",
        payload=payload,
        extra_headers=headers,
        timeout=620,
    )
    run_id = str(created.get("run_id", ""))
    if not run_id:
        raise CommandError("Proof-run creation did not return run_id.")

    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        run = _request_object(f"{app_url}/api/runs/{run_id}")
        status = str(run.get("status", ""))
        if status == "completed":
            manifest_ref = run.get("manifest_ref")
            report_ref = run.get("report_ref")
            if (
                not isinstance(manifest_ref, str)
                or not manifest_ref.startswith("/api/artifacts/")
                or not isinstance(report_ref, str)
                or not report_ref.startswith("/api/artifacts/")
            ):
                raise CommandError("Completed proof run is missing manifest or report evidence.")
            manifest = _verified_artifact_object(app_url, manifest_ref)
            manifest_solutions = manifest.get("solution_ids")
            if (
                manifest.get("run_id") != run_id
                or manifest.get("networked_execution") is not True
                or not isinstance(manifest_solutions, list)
                or set(manifest_solutions) != set(solutions)
            ):
                raise CommandError(
                    "Proof manifest does not pin the requested run and networked execution."
                )
            report = _verified_artifact_object(app_url, report_ref)
            report_api = _request_object(f"{app_url}/api/runs/{run_id}/report")
            manifest_match = ARTIFACT_REF.fullmatch(manifest_ref)
            if (
                report != report_api
                or report.get("run_id") != run_id
                or manifest_match is None
                or report.get("manifest_artifact_id") != manifest_match.group(1)
            ):
                raise CommandError(
                    "Proof report artifact, API response, and manifest identity do not agree."
                )
            report_results = report.get("results")
            if not isinstance(report_results, list):
                raise CommandError("Proof report does not contain a result list.")
            completed_results = [
                result
                for result in report_results
                if isinstance(result, dict)
                and result.get("solution_id") in solutions
                and result.get("status") == "succeeded"
                and result.get("metrics")
                and isinstance(result.get("telemetry"), dict)
                and result["telemetry"].get("provider_request_id")
                and result.get("evidence")
            ]
            if not completed_results:
                raise CommandError(
                    "Proof report lacks a successful evaluated provider result with telemetry."
                )
            if dataset_id:
                if (
                    manifest.get("dataset_revision") != dataset.get("revision")
                    or manifest.get("retry_limit") != 3
                    or len(manifest.get("sample_ids", [])) != 2
                    or run.get("dataset_id") != dataset_id
                    or run.get("expires_at") != dataset.get("expires_at")
                    or run.get("pinned") is not False
                ):
                    raise CommandError(
                        "Hosted proof manifest/selection/retention differs from its dataset."
                    )
                if len(report_results) != 10 or len(completed_results) != 10:
                    raise CommandError("Hosted proof requires all ten evaluated provider results.")
                for identifier in solutions:
                    if (
                        sum(item.get("solution_id") == identifier for item in completed_results)
                        != 2
                    ):
                        raise CommandError(
                            "Hosted proof does not contain the exact two-by-five matrix."
                        )
                for result in completed_results:
                    roles = {item.get("role") for item in result.get("evidence", [])}
                    if not {"input", "raw", "parsed", "comparison", "result"}.issubset(roles):
                        raise CommandError(
                            "A hosted proof result lacks complete evaluated evidence."
                        )
                    for item in result.get("evidence", []):
                        _verified_artifact_bytes(app_url, f"/api/artifacts/{item['artifact_id']}")
                inputs = {
                    item["artifact_id"]: item
                    for result in completed_results
                    for item in result.get("evidence", [])
                    if item.get("role") == "input"
                }
                for item in inputs.values():
                    _verify_audio_delivery(app_url, item)
                histories = report.get("attempt_history", {})
                if not isinstance(histories, dict) or len(histories) != 10:
                    raise CommandError("Hosted proof requires all ten attempt histories.")
                for history in histories.values():
                    if not isinstance(history, list) or not 1 <= len(history) <= 3:
                        raise CommandError("Hosted proof exceeds the three-total-attempt ceiling.")
                    for attempt in history:
                        for item in attempt.get("evidence", []):
                            _verified_artifact_bytes(
                                app_url, f"/api/artifacts/{item['artifact_id']}"
                            )
                snapshot_id = manifest.get("sample_snapshot_artifact_id")
                if not isinstance(snapshot_id, str):
                    raise CommandError("Hosted proof is missing its immutable sample snapshot.")
                _verified_artifact_bytes(app_url, f"/api/artifacts/{snapshot_id}")
            proof_result = completed_results[0]
            evidence = proof_result["evidence"]
            roles = {item.get("role") for item in evidence if isinstance(item, dict)}
            required_roles = {"input", "raw", "parsed", "comparison", "result"}
            if not required_roles.issubset(roles):
                raise CommandError(
                    "Proof result lacks required input, raw, parsed, comparison, "
                    "or result evidence."
                )
            for item in evidence:
                if not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str):
                    raise CommandError("Proof result contains an invalid evidence record.")
                _verified_artifact_bytes(app_url, f"/api/artifacts/{item['artifact_id']}")
            attempts = report.get("attempt_history")
            task_attempts = (
                attempts.get(str(proof_result.get("task_id")))
                if isinstance(attempts, dict)
                else None
            )
            if not isinstance(task_attempts, list) or not any(
                isinstance(attempt, dict)
                and isinstance(attempt.get("telemetry"), dict)
                and attempt["telemetry"].get("provider_request_id")
                for attempt in task_attempts
            ):
                raise CommandError("Proof report lacks attempt-scoped provider telemetry.")
            attempt_evidence = [
                item
                for attempt in task_attempts
                if isinstance(attempt, dict)
                for item in attempt.get("evidence", [])
                if isinstance(item, dict)
            ]
            if not attempt_evidence:
                raise CommandError("Proof report lacks attempt-scoped artifacts.")
            for item in attempt_evidence:
                artifact_id = item.get("artifact_id")
                if not isinstance(artifact_id, str):
                    raise CommandError("Proof attempt contains an invalid evidence record.")
                _verified_artifact_bytes(app_url, f"/api/artifacts/{artifact_id}")
            print(f"Proof run completed with immutable evidence: {run_id}")
            return
        if status in {"failed", "cancelled", "poisoned"}:
            raise CommandError(f"Proof run reached terminal status {status}: {run_id}")
        time.sleep(5)
    raise CommandError(f"Timed out waiting for proof run {run_id}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an AI Bench Azure deployment.")
    parser.add_argument("--require-proof", action="store_true")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Read-only initial wiring check; never readiness or completed deployment proof.",
    )
    parser.add_argument("--benchmark")
    parser.add_argument("--solution", action="append")
    parser.add_argument("--dataset-id")
    parser.add_argument("--proof-state", type=Path)
    arguments = parser.parse_args()
    if arguments.bootstrap and arguments.require_proof:
        raise CommandError("--bootstrap cannot launch paid proof or certify release readiness.")
    if (
        arguments.dataset_id or arguments.benchmark or arguments.solution or arguments.proof_state
    ) and not arguments.require_proof:
        raise CommandError("Proof selectors require explicit --require-proof authorization.")

    require_command("az")
    require_command("azd")
    subscription_id = str(azd_value("AZURE_SUBSCRIPTION_ID"))
    resource_group = str(azd_value("AZURE_RESOURCE_GROUP"))
    app_name = str(azd_value("AI_BENCH_APP_NAME"))
    app_url = str(azd_value("AI_BENCH_APP_URL")).rstrip("/")
    if not app_url.startswith("https://"):
        raise CommandError("AI_BENCH_APP_URL must use HTTPS.")
    storage_name = str(azd_value("AI_BENCH_STORAGE_ACCOUNT"))
    vault_name = str(azd_value("AI_BENCH_KEY_VAULT_NAME"))
    auth_mode = str(azd_value("AI_BENCH_AUTH_MODE")).casefold()
    foundry_scope = str(azd_value("AI_BENCH_FOUNDRY_SCOPE") or "")
    public_demo = azd_value("AI_BENCH_PUBLIC_DEMO_MODE", required=False) == "true"
    endpoint_ids = json.loads(str(azd_value("AI_BENCH_PRIVATE_ENDPOINT_IDS")))
    if not isinstance(endpoint_ids, list) or not all(
        isinstance(value, str) for value in endpoint_ids
    ):
        raise CommandError("Private endpoint deployment output must be an array of exact IDs.")
    _verify_arm_state(
        subscription_id,
        resource_group,
        app_name,
        storage_name,
        vault_name,
        auth_mode,
        foundry_scope,
        mai_scope=str(azd_value("AI_BENCH_MAI_SCOPE", required=False) or ""),
        public_demo=public_demo,
        scm_cidr=str(azd_value("AI_BENCH_SCM_ALLOWED_CIDR")),
        expected_endpoint_ids=tuple(endpoint_ids),
    )

    _verify_kudu(app_name, subscription_id)
    _wait_endpoint(f"{app_url}/health", "ok")
    if arguments.bootstrap:
        runtime = _request_object(f"{app_url}/api/runtime")
        if (
            runtime.get("runtime") != "azure"
            or runtime.get("execution_backend") != "azure-table-blob"
            or runtime.get("public_demo") is not public_demo
        ):
            raise CommandError(
                "Bootstrap runtime does not use the configured Azure Table/Blob backend."
            )
        print(
            "Read-only bootstrap wiring checked: ARM configuration, SCM, health and "
            "selected runtime. NOT deployment proof. Readiness, worker MI/data-plane "
            "conformance, baseline publication and full hosted proof "
            "are not verified by this check."
        )
        return 0
    _wait_endpoint(f"{app_url}/ready", "ready")

    if arguments.require_proof:
        if not arguments.benchmark or not arguments.solution:
            raise CommandError("--require-proof also requires --benchmark and --solution.")
        if public_demo and (not arguments.dataset_id or not arguments.proof_state):
            raise CommandError(
                "Public proof requires --dataset-id and --proof-state for one "
                "resumable two-pair synthetic run."
            )
        _proof_run(
            app_url,
            arguments.benchmark,
            arguments.solution,
            dataset_id=arguments.dataset_id,
            proof_state=arguments.proof_state,
        )

    print(
        "Verified ARM identity/configuration, Kudu, health, and readiness."
        + (" Real provider proof also completed." if arguments.require_proof else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
