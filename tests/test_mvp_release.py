from __future__ import annotations

import copy
import json
import subprocess
import urllib.error
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from ai_bench import backend_proof
from ai_bench.azure_artifacts import AzureBlobArtifactStore
from ai_bench.backend_proof import (
    REQUIRED_SCENARIOS,
    BackendConformanceReceipt,
    QualificationRequiredError,
    backend_code_signature,
    publish_receipt,
    verify_receipt,
)
from ai_bench.contracts import utc_now
from ai_bench.storage_state import AzureTableStateStore, StateChange
from infra.scripts import (
    hosted_checks,
    preflight,
    prepare_environment,
    preservation,
    release,
    release_runtime,
)
from infra.scripts._azure import CommandError
from tests.test_core_blob import FakeContainer
from tests.test_hosted_core import FakeTable
from tests.test_hosted_infra import topology

NEW_SCENARIOS = {
    "bounded-parallel-single-owner",
    "global-solution-lanes-fast-progress",
    "same-run-parallel-evidence-events",
    "eligible-lanes-beyond-first-page",
}


def test_legacy_seventeen_scenarios_do_not_qualify_parallel_backend(tmp_path: Path) -> None:
    state = AzureTableStateStore(FakeTable(), "conformance-old", match_condition="IfNotModified")
    objects = AzureBlobArtifactStore(
        FakeContainer(),
        state,
        tmp_path,
        object_namespace="conformance-old",
        match_condition="IfNotModified",
    )
    old = BackendConformanceReceipt(
        code_signature="c" * 64,
        configuration_signature="a" * 64,
        source_namespace="conformance-old",
        completed_at=utc_now(),
        scenarios=tuple(sorted(REQUIRED_SCENARIOS - NEW_SCENARIOS)),
    )
    assert len(old.scenarios) == 17 and len(REQUIRED_SCENARIOS) == 21
    proof_state, proof_objects = backend_proof._proof_store(state, objects)
    artifact = proof_objects.put_json(old)
    proof_state.transact((StateChange(f"checkpoint:{artifact.id}", {"artifact_id": artifact.id}),))
    with pytest.raises(QualificationRequiredError, match="scenarios"):
        verify_receipt(state, objects, artifact.id, "a" * 64)
    with pytest.raises(ValueError, match="does not match"):
        verify_receipt(state, objects, artifact.id, "b" * 64)


@pytest.mark.parametrize(
    "field,value",
    [
        ("suite_version", "future-suite"),
        ("identity", "key"),
        ("backend", "sqlite"),
    ],
)
def test_unknown_or_wrong_qualification_fails_closed(
    tmp_path: Path, field: str, value: str
) -> None:
    state = AzureTableStateStore(FakeTable(), "conformance-proof", match_condition="IfNotModified")
    objects = AzureBlobArtifactStore(
        FakeContainer(), state, tmp_path, match_condition="IfNotModified"
    )
    checkpoint = BackendConformanceReceipt(
        code_signature=backend_code_signature(),
        configuration_signature="a" * 64,
        source_namespace="conformance-proof",
        completed_at=utc_now(),
        scenarios=tuple(sorted(REQUIRED_SCENARIOS)),
    )
    digest = publish_receipt(state, objects, checkpoint)
    assert verify_receipt(state, objects, digest, "a" * 64)
    proof_state, proof_objects = backend_proof._proof_store(state, objects)
    payload = checkpoint.model_dump(mode="json")
    payload[field] = value
    artifact = proof_objects.put_json(payload)
    proof_state.transact((StateChange(f"checkpoint:{artifact.id}", {"artifact_id": artifact.id}),))
    with pytest.raises(ValueError):
        verify_receipt(state, objects, artifact.id, "a" * 64)
    assert not verify_receipt(state, objects, "", "a" * 64)
    with pytest.raises(ValueError, match="not been published"):
        verify_receipt(state, objects, "f" * 64, "a" * 64)


def test_public_demo_requires_explicit_empty_or_bounded_scm_setting() -> None:
    for explicit in ("", "192.0.2.1/32"):
        preflight._validate_execution_exposure("entra", "true", "true", "", "true", explicit)
    for auth, demo, scm in (
        ("entra", "true", None),
        ("entra", "false", ""),
        ("key", "true", ""),
        ("entra", "true", "0.0.0.0/0"),
        ("entra", "true", "not-a-cidr"),
    ):
        with pytest.raises(CommandError):
            preflight._validate_execution_exposure(auth, "true", "true", "", demo, scm)


def test_entra_scm_policy_verifies_basic_auth_and_private_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_hosted_infra import verify

    original = hosted_checks.verify_arm

    def entra(*args: Any, **kwargs: Any) -> str:
        return original(*args, **{**kwargs, "scm_cidr": ""})

    monkeypatch.setattr(hosted_checks, "verify_arm", entra)
    value = topology()
    value["config"]["scmIpSecurityRestrictionsDefaultAction"] = "Allow"
    value["config"]["scmIpSecurityRestrictions"] = []
    assert verify(value) == "app-principal"
    mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda v: v["storage"].update(publicNetworkAccess="Enabled"),
        lambda v: v["config"].update(scmIpSecurityRestrictionsUseMain=True),
        lambda v: v["config"].update(scmIpSecurityRestrictionsDefaultAction="Deny"),
    )
    for mutate in mutations:
        broken = copy.deepcopy(value)
        mutate(broken)
        with pytest.raises(CommandError):
            verify(broken)
    policy = next(
        key for key in value["rest"] if key.endswith("basicPublishingCredentialsPolicies/scm")
    )
    value["rest"][policy]["properties"]["allow"] = True
    with pytest.raises(CommandError, match="Basic"):
        verify(value)


def test_only_exact_scm_network_delta_is_allowed() -> None:
    before = topology()["config"]
    after = {
        **before,
        "scmIpSecurityRestrictions": [],
        "scmIpSecurityRestrictionsDefaultAction": "Allow",
        "scmIpSecurityRestrictionsUseMain": False,
    }
    release.check_scm_delta(before, after)
    with pytest.raises(CommandError, match="beyond"):
        release.check_scm_delta(before, {**after, "minimumElasticInstanceCount": 1})
    with pytest.raises(CommandError, match="Unexpected"):
        release.check_scm_delta(
            before,
            {
                **after,
                "scmIpSecurityRestrictions": [{"action": "Allow", "ipAddress": "192.0.2.1/32"}],
            },
        )


def test_qualification_is_required_for_backend_changes_not_ui() -> None:
    before: dict[str, Any] = {
        "python": [3, 12],
        "versions": {"pydantic": "2.11.7"},
        "files": {
            name: "a" * 64
            for name in (
                *backend_proof.qualification_sources(),
                "requirements.lock",
                "infra/scripts/release_runtime.py",
                "src/ai_bench/web/static/index.html",
            )
        },
    }
    candidate = {**copy.deepcopy(before), "qualification_required": False}
    assert not release.qualification_needed(before, candidate)
    candidate["files"]["src/ai_bench/web/static/index.html"] = "b" * 64
    assert not release.qualification_needed(before, candidate)
    for changed in ("src/ai_bench/worker.py", "src/ai_bench/storage_state.py", "requirements.lock"):
        updated = copy.deepcopy(candidate)
        updated["files"][changed] = "b" * 64
        assert release.qualification_needed(before, updated)
    assert release.qualification_needed(before, {**candidate, "qualification_required": True})
    assert release.qualification_needed(before, {**candidate, "python": [3, 13]})
    assert release.qualification_needed(before, {**candidate, "versions": {"pydantic": "3.0"}})


def operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> release.Release:
    op = object.__new__(release.Release)
    op.evidence, op.entra_only, op.namespace = tmp_path / "release", False, None
    op.url, op.id, op.scm = (
        "https://app.example.test",
        "synthetic-app",
        "https://scm.example.test",
    )
    op.record = {}
    monkeypatch.setattr(op, "auth_guards", lambda: None)
    monkeypatch.setattr(
        op, "configuration", lambda: {"scmIpSecurityRestrictionsDefaultAction": "Deny"}
    )
    monkeypatch.setattr(op, "fingerprint", lambda: {"setting": "a" * 64})
    monkeypatch.setattr(release, "source_identity", lambda: {"head": "b" * 40})
    monkeypatch.setattr(release, "idle", lambda _: [])
    return op


def test_dry_run_never_builds_writes_or_opens_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = operation(tmp_path, monkeypatch)

    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("dry run attempted a mutation or operator transport")

    for name in ("package", "Runtime", "run"):
        monkeypatch.setattr(release, name, forbidden)
    for name in ("save", "resources", "enable_scm", "set_checkpoint", "deploy_once"):
        monkeypatch.setattr(op, name, forbidden)
    assert op.execute(dry_run=True)["dry_run"]
    assert not op.evidence.exists()


def test_active_work_blocks_release_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = operation(tmp_path, monkeypatch)
    monkeypatch.undo()
    monkeypatch.setattr(release, "json_read", lambda _: [{"status": "running"}])
    with pytest.raises(CommandError, match="Active runs"):
        release.idle(op.url)
    assert not op.evidence.exists()


@pytest.mark.parametrize("kudu_success", [False, True])
def test_failed_deployment_is_not_retried_without_an_orphaned_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kudu_success: bool
) -> None:
    op = operation(tmp_path, monkeypatch)
    op.evidence.mkdir()
    archive = tmp_path / "candidate.zip"
    archive.write_bytes(b"synthetic archive")
    calls: list[list[str]] = []
    responses = iter(
        (
            {"id": "old", "status": 4, "complete": True, "active": True},
            {
                "id": "new",
                "status": 4 if kudu_success else 3,
                "complete": True,
                "active": kudu_success,
            },
        )
    )
    monkeypatch.setattr(op, "kudu", lambda _: next(responses))
    monkeypatch.setattr(op, "release_orphaned_lock", lambda: None)

    def lost_response(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "lost deploy response")

    monkeypatch.setattr(release, "run", lost_response)
    if kudu_success:
        op.deploy_once(archive)
        assert op.record["deployment_command_exit"] == 1
    else:
        with pytest.raises(CommandError, match="retain state and inspect"):
            op.deploy_once(archive)
    assert len(calls) == 1
    with pytest.raises(CommandError, match="already attempted"):
        op.deploy_once(archive)
    assert len(calls) == 1
    assert json.loads((op.evidence / "deployment-started.json").read_bytes())["zip_sha256"]


def test_interrupted_build_resends_the_identical_package_after_releasing_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = operation(tmp_path, monkeypatch)
    op.evidence.mkdir()
    archive = tmp_path / "candidate.zip"
    archive.write_bytes(b"synthetic archive")
    responses = iter(
        (
            {"id": "old", "status": 4, "complete": True, "active": True},
            {"id": "interrupted", "status": 3, "complete": True, "active": False},
            {"id": "recovered", "status": 4, "complete": True, "active": True},
        )
    )
    monkeypatch.setattr(op, "kudu", lambda _: next(responses))
    released: list[dict[str, Any]] = []

    def release_lock() -> dict[str, Any]:
        released.append({"heldByPID": 840})
        return released[-1]

    monkeypatch.setattr(op, "release_orphaned_lock", release_lock)
    calls: list[list[str]] = []

    def deploy(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release, "run", deploy)
    op.deploy_once(archive)
    # The recovery must resend the same qualified bytes, never a rebuilt package.
    assert len(calls) == 2 and calls[0] == calls[1] and len(released) == 1
    attempts = op.record["deployment_attempts"]
    assert [item["deployment_id"] for item in attempts] == ["interrupted", "recovered"]
    assert attempts[0]["released_orphaned_lock"] == {"heldByPID": 840}


@pytest.mark.parametrize(
    "status,location,accepted",
    [
        (401, "", True),
        (302, "https://login.microsoftonline.com/common/oauth2/authorize", True),
        (302, "https://untrusted.example.test/login", False),
        (403, "", False),
        (200, "", False),
    ],
)
def test_scm_anonymous_check_does_not_follow_login_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    location: str,
    accepted: bool,
) -> None:
    op = operation(tmp_path, monkeypatch)
    monkeypatch.setattr(op, "kudu", lambda _: {"id": "authenticated"})
    calls: list[str] = []

    def response(url: str, **_: Any) -> bytes:
        calls.append(url)
        if status == 200:
            return b'{"id":"unauthenticated"}'
        headers = Message()
        if location:
            headers["Location"] = location
        raise urllib.error.HTTPError(url, status, "synthetic", headers, None)

    monkeypatch.setattr(release, "http", response)
    if accepted:
        op.scm_auth()
    else:
        with pytest.raises(CommandError):
            op.scm_auth()
    assert calls == [op.scm + "/api/deployments/latest"]
    assert release.NoRedirect().redirect_request(None, None, 302, "", {}, location) is None


def test_kudu_refreshes_operator_token_for_each_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = operation(tmp_path, monkeypatch)
    op.values = {"AZURE_SUBSCRIPTION_ID": "synthetic"}
    tokens = iter(("first", "second"))
    seen: list[str] = []
    monkeypatch.setattr(release, "az_json", lambda _: {"accessToken": next(tokens)})

    def read(_: str, *, token: str) -> dict[str, str]:
        seen.append(token)
        return {"id": "deployment"}

    monkeypatch.setattr(release, "json_read", read)
    op.kudu("/api/deployments/latest")
    op.kudu("/api/deployments/latest")
    assert seen == ["first", "second"]


@pytest.mark.parametrize("incomplete", [False, True])
def test_complete_release_keeps_compatible_setting_and_verifies_live_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, incomplete: bool
) -> None:
    op = operation(tmp_path, monkeypatch)
    op.app = {"identity": {"principalId": "synthetic-principal"}}
    op.selection = []
    op.values = {release.CHECKPOINT_SETTING: "a" * 64}
    op.namespace = "conformance-invented" if incomplete else None
    files = {
        name: "c" * 64
        for name in (
            *backend_proof.qualification_sources(),
            "requirements.lock",
            "infra/scripts/release_runtime.py",
        )
    }
    base = {
        "files": files,
        "python": [3, 12],
        "versions": {"pydantic": "2.11.7"},
        "code_signature": "d" * 64,
        "configuration_signature": "e" * 64,
        "active_runs": [],
    }
    candidate = {**base, "qualification_required": incomplete}
    actions: list[str] = []

    def create_package(directory: Path) -> tuple[Path, dict[str, str]]:
        archive = directory / "application.zip"
        archive.write_bytes(b"synthetic reviewed ZIP")
        return archive, files

    class Transport:
        def __init__(self, *_: Any) -> None:
            pass

        def __enter__(self) -> Transport:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def serving(self, remote: str) -> dict[str, Any]:
            # New user work is allowed during post-deploy read-only verification.
            return {**base, "active_runs": ["new-user-run"] if remote.endswith("-after") else []}

        def stage(self, *_: Any) -> str:
            actions.append("stage")
            return "/candidate"

        def candidate(self, action: str, *_: Any, **__: Any) -> dict[str, Any]:
            actions.append(action)
            if action == "qualify":
                return {**candidate, "qualification": {"receipt_digest": "f" * 64}}
            return candidate

    current_settings = {"setting": "a" * 64}

    def set_proof(digest: str, before: dict[str, str]) -> None:
        actions.append("setting")
        assert before == current_settings
        current_settings[release.CHECKPOINT_SETTING] = release.sha(digest.encode())

    monkeypatch.setattr(release, "package", create_package)
    monkeypatch.setattr(release, "Runtime", Transport)
    monkeypatch.setattr(release, "run", lambda *_, **__: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(op, "scm_auth", lambda: None)
    monkeypatch.setattr(op, "resources", lambda: {"preservation": {}, "inventory": {}, "site": {}})
    monkeypatch.setattr(preservation, "compare", lambda *_: None)
    monkeypatch.setattr(op, "reports", lambda _: {"old": {"report": "c" * 64}})
    monkeypatch.setattr(op, "fingerprint", lambda: dict(current_settings))
    monkeypatch.setattr(op, "set_checkpoint", set_proof)
    monkeypatch.setattr(op, "deploy_once", lambda _: actions.append("deploy"))
    result = op.execute(dry_run=False)
    assert result["status"] == "verified"
    assert result["active_runs_after"] == ["new-user-run"]
    assert actions == (
        ["stage", "inspect", "qualify", "setting", "deploy"]
        if incomplete
        else ["stage", "inspect", "deploy"]
    )


def test_dirty_namespace_never_starts_conformance(
    tmp_path: Path, pack_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_hosted_core import engine_for

    state = AzureTableStateStore(
        FakeTable(), "conformance-invented", match_condition="IfNotModified"
    )
    engine = engine_for(state, tmp_path, pack_root)
    state.transact((StateChange("already-used", {"existing": True}),))
    monkeypatch.setenv("AI_BENCH_AZURE_NAMESPACE", "application")
    monkeypatch.setattr(release_runtime, "examine", lambda *_, **__: {"active_runs": []})
    monkeypatch.setattr(release_runtime, "azure_backend", lambda *_, **__: engine.backend)
    monkeypatch.setattr(
        release_runtime,
        "run_conformance",
        lambda *_, **__: pytest.fail("dirty namespace must not execute or delete anything"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "runtime",
            "qualify",
            "--root",
            str(tmp_path),
            "--state-root",
            str(tmp_path),
            "--worker-pid",
            "1",
            "--namespace",
            "conformance-invented",
        ],
    )
    with pytest.raises(ValueError, match="not empty"):
        release_runtime.main()
    assert state.get("already-used") is not None


def test_candidate_cannot_reuse_application_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_BENCH_AZURE_NAMESPACE", "conformance-existing")
    monkeypatch.setattr(release_runtime, "examine", lambda *_, **__: {"active_runs": []})
    monkeypatch.setattr(
        "sys.argv",
        [
            "runtime",
            "qualify",
            "--root",
            str(tmp_path),
            "--state-root",
            str(tmp_path),
            "--worker-pid",
            "1",
            "--namespace",
            "conformance-existing",
        ],
    )
    with pytest.raises(ValueError, match="application namespace"):
        release_runtime.main()


def test_package_read_rejects_changed_bytes_and_unsafe_paths(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_bytes(b"synthetic\n")
    entry = {"path": "source.py", "bytes": 10, "sha256": release.sha(b"synthetic\n")}
    manifest = tmp_path / "package-manifest.json"
    manifest.write_text(json.dumps({"files": [entry]}))
    (tmp_path / "requirements.lock").write_bytes(b"")
    (tmp_path / "requirements.txt").write_bytes(b"")
    assert release_runtime.inspect_package(tmp_path) == {"source.py": entry["sha256"]}
    (tmp_path / "source.py").write_bytes(b"altered!!!")
    with pytest.raises(ValueError, match="differs"):
        release_runtime.inspect_package(tmp_path)
    manifest.write_text(json.dumps({"files": [{**entry, "path": "../source.py"}]}))
    with pytest.raises(ValueError, match="invalid package"):
        release_runtime.inspect_package(tmp_path)


def test_accepted_source_checks_use_github_native_json_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40

    def command(args: list[str]) -> subprocess.CompletedProcess[str]:
        assert "--output" not in args
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            output = head
        elif args[:2] == ["git", "rev-parse"]:
            output = "b" * 40
        elif args[:2] == ["git", "status"]:
            output = " M .azure/deployment-plan.md\n"
        elif args[-1] == "nameWithOwner":
            output = json.dumps({"nameWithOwner": "example/repository"})
        elif args[-1] == "defaultBranchRef":
            output = json.dumps({"defaultBranchRef": {"name": "main"}})
        elif args[:2] == ["gh", "api"]:
            output = json.dumps({"commit": {"sha": head}})
        elif args[:3] == ["gh", "run", "list"]:
            output = json.dumps(
                [
                    {
                        "headSha": head,
                        "status": "completed",
                        "conclusion": "success",
                        "databaseId": 1,
                        "url": "https://github.example.test/run/1",
                    }
                ]
            )
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(release, "run", command)
    result = release.source_identity()
    assert result["head"] == head and result["ci"]["conclusion"] == "success"


def test_environment_recovery_preserves_explicit_entra_only_scm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def read(command: list[str]) -> Any:
        if command[0] == "resource":
            return [{"name": "app", "tags": {"azd-env-name": "demo", "azd-service-name": "app"}}]
        if command[:3] == ["webapp", "config", "appsettings"]:
            return [
                {"name": key, "value": value}
                for key, value in {
                    "AI_BENCH_PUBLIC_DEMO_MODE": "true",
                    "AI_BENCH_AUTH_MODE": "entra",
                    "AI_BENCH_KEY_VAULT_NAME": "vault",
                }.items()
            ]
        if command[:2] == ["webapp", "show"]:
            return {"serverFarmId": "synthetic-plan", "publicNetworkAccess": "Enabled"}
        if command[0] == "rest":
            return {"sku": {"name": "B1"}}
        return {
            "ipSecurityRestrictions": [],
            "ipSecurityRestrictionsDefaultAction": "Allow",
            "scmIpSecurityRestrictions": [{"action": "Allow", "ipAddress": "Any"}],
            "scmIpSecurityRestrictionsDefaultAction": "Allow",
            "scmIpSecurityRestrictionsUseMain": False,
        }

    monkeypatch.setattr(prepare_environment, "az_json", read)
    monkeypatch.setattr(prepare_environment, "azd_value", lambda *_, **__: None)
    result = prepare_environment._deployed_optional_values("synthetic", "group", "demo")
    assert result["AI_BENCH_SCM_ALLOWED_CIDR"] == ""


def test_idle_observation_retries_transient_recycle_but_never_assumes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: list[object] = [
        urllib.error.HTTPError("https://app.example.test", 503, "recycling", Message(), None),
        [],
    ]
    sleeps: list[int] = []

    def read(_: str) -> Any:
        value = replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(release, "json_read", read)
    monkeypatch.setattr("infra.scripts.release.time.sleep", sleeps.append)
    assert release.idle_after_recycle("https://app.example.test") == []
    assert sleeps == [1]
    replies.append([{"status": "running"}])
    with pytest.raises(CommandError, match="Active"):
        release.idle_after_recycle("https://app.example.test")
    assert sleeps == [1]


def test_unavailable_idle_observation_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def unavailable(_: str) -> Any:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError("https://app.example.test", 503, "recycling", Message(), None)

    monkeypatch.setattr(release, "json_read", unavailable)
    monkeypatch.setattr("infra.scripts.release.time.sleep", lambda _: None)
    with pytest.raises(urllib.error.HTTPError):
        release.idle_after_recycle("https://app.example.test")
    assert calls == 6


def test_already_open_scm_repairs_only_local_selected_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = operation(tmp_path, monkeypatch)
    op.entra_only = True
    before = {
        "scmIpSecurityRestrictionsDefaultAction": "Allow",
        "scmIpSecurityRestrictionsUseMain": False,
        "scmIpSecurityRestrictions": [],
    }
    monkeypatch.setattr(op, "configuration", lambda: before)
    commands: list[list[str]] = []

    def local_set(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release, "run", local_set)
    monkeypatch.setattr(
        release, "az_json", lambda _: pytest.fail("already matching ARM must not be mutated")
    )
    op.enable_scm(before, {"setting": "a" * 64})
    assert commands == [["azd", "env", "set", "AI_BENCH_SCM_ALLOWED_CIDR", ""]]
