from __future__ import annotations

import hashlib
import json
import os
import shutil
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from infra.scripts import (
    _azure,
    preflight,
    prepare_environment,
    stage_package,
    sync_auth_mode,
    verify_deployment,
)
from infra.scripts._azure import CommandError


def test_command_runner_resolves_windows_command_shims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[list[str]] = []
    launcher = tmp_path / "CLI2" / "wbin" / "az.CMD"
    bundled_python = tmp_path / "CLI2" / "python.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off\n", encoding="ascii")
    bundled_python.write_bytes(b"")
    monkeypatch.setattr(
        "infra.scripts._azure.shutil.which",
        lambda _name: str(launcher),
    )

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        observed.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout="{}")

    monkeypatch.setattr("infra.scripts._azure.subprocess.run", fake_run)

    _azure.run(["az", "account", "show"])

    assert observed == [[str(bundled_python), "-IBm", "azure.cli", "account", "show"]]


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("az") is None,
    reason="requires the installed Windows Azure CLI launcher",
)
def test_windows_azure_launcher_preserves_shell_metacharacters() -> None:
    marker = "AI_BENCH_ARGUMENT_SPLIT"
    result = _azure.run(
        ["az", "version", "--query", f"az&echo.{marker}"],
        check=False,
    )
    assert marker not in result.stdout.splitlines()


def test_deployer_lookup_uses_selected_subscription_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    graph_requests: list[tuple[str, str]] = []
    monkeypatch.delenv("AZURE_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("AZURE_PRINCIPAL_TYPE", raising=False)

    def fake_az_json(command: list[str]) -> dict[str, Any]:
        commands.append(command)
        return {
            "id": "selected-subscription",
            "user": {"name": "operator@example.test", "type": "user"},
        }

    def fake_graph_json(url: str, subscription_id: str) -> dict[str, str]:
        graph_requests.append((url, subscription_id))
        return {"id": "user-object-id"}

    monkeypatch.setattr(_azure, "az_json", fake_az_json)
    monkeypatch.setattr(_azure, "_graph_json", fake_graph_json)

    assert _azure.deployer_principal("selected-subscription") == (
        "user-object-id",
        "User",
    )
    assert commands[0][-2:] == ["--subscription", "selected-subscription"]
    assert all("ad" not in command for command in commands)
    assert graph_requests == [
        (
            "https://graph.microsoft.com/v1.0/me?%24select=id",
            "selected-subscription",
        )
    ]


def test_service_principal_lookup_uses_subscription_scoped_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    graph_requests: list[tuple[str, str]] = []
    monkeypatch.delenv("AZURE_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("AZURE_PRINCIPAL_TYPE", raising=False)

    def fake_az_json(command: list[str]) -> dict[str, Any]:
        commands.append(command)
        return {
            "id": "selected-subscription",
            "user": {"name": "application-id", "type": "servicePrincipal"},
        }

    def fake_graph_json(url: str, subscription_id: str) -> dict[str, Any]:
        graph_requests.append((url, subscription_id))
        return {"value": [{"id": "service-principal-object-id"}]}

    monkeypatch.setattr(_azure, "az_json", fake_az_json)
    monkeypatch.setattr(_azure, "_graph_json", fake_graph_json)

    assert _azure.deployer_principal("selected-subscription") == (
        "service-principal-object-id",
        "ServicePrincipal",
    )
    assert "servicePrincipals" in graph_requests[0][0]
    assert graph_requests[0][1] == "selected-subscription"


def test_graph_token_uses_canonical_scope_in_selected_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    requests: list[Request] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"id":"selected-tenant-user"}'

    def fake_az_json(command: list[str]) -> dict[str, str]:
        commands.append(command)
        return {"accessToken": "selected-tenant-token"}

    def fake_urlopen(request: Request, *, timeout: float) -> Response:
        assert timeout == 30
        requests.append(request)
        return Response()

    monkeypatch.setattr(_azure, "az_json", fake_az_json)
    monkeypatch.setattr("infra.scripts._azure.urlopen", fake_urlopen)

    assert _azure._graph_json(
        "https://graph.microsoft.com/v1.0/me?%24select=id",
        "selected-subscription",
    ) == {"id": "selected-tenant-user"}
    assert commands == [
        [
            "account",
            "get-access-token",
            "--subscription",
            "selected-subscription",
            "--scope",
            "https://graph.microsoft.com/.default",
        ]
    ]
    authorization = requests[0].get_header("Authorization")
    assert authorization is not None
    assert authorization.startswith("Bearer ")
    assert authorization.removeprefix("Bearer ") == "selected-tenant-token"


@pytest.mark.parametrize("status", [401, 403])
def test_graph_rejection_does_not_retry_or_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    commands: list[list[str]] = []
    requests: list[Request] = []

    def fake_az_json(command: list[str]) -> dict[str, str]:
        commands.append(command)
        return {"accessToken": "selected-tenant-token"}

    def reject(request: Request, *, timeout: float) -> None:
        assert timeout == 30
        requests.append(request)
        raise HTTPError(request.full_url, status, "Access denied", Message(), None)

    monkeypatch.setattr(_azure, "az_json", fake_az_json)
    monkeypatch.setattr("infra.scripts._azure.urlopen", reject)

    with pytest.raises(CommandError, match="Microsoft Graph identity lookup failed") as failure:
        _azure._graph_json("https://graph.microsoft.com/v1.0/me", "selected-subscription")

    assert len(commands) == len(requests) == 1
    assert commands[0][-4:] == [
        "--subscription",
        "selected-subscription",
        "--scope",
        "https://graph.microsoft.com/.default",
    ]
    assert isinstance(failure.value.__cause__, HTTPError)
    assert failure.value.__cause__.code == status


def test_initial_mode_state_requires_arm_proof_of_new_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def absent(command: list[str]) -> Any:
        if command[:2] == ["keyvault", "show"]:
            raise CommandError("(ResourceNotFound) exact target is absent")
        return []

    monkeypatch.setattr(prepare_environment, "az_json", absent)
    assert (
        prepare_environment._initial_mode_state(
            "subscription",
            "environment",
            "selected-existing-group",
            "",
            "exact-target-vault",
        )
        == "fresh"
    )

    monkeypatch.setattr(
        prepare_environment,
        "az_json",
        lambda _command: {"name": "exact-target-vault"},
    )
    assert (
        prepare_environment._initial_mode_state(
            "subscription",
            "environment",
            "selected-existing-group",
            "",
            "exact-target-vault",
        )
        == "unknown"
    )
    assert (
        prepare_environment._initial_mode_state(
            "subscription",
            "environment",
            "reused-resource-group",
            "",
        )
        == "unknown"
    )
    monkeypatch.setattr(
        prepare_environment,
        "az_json",
        lambda _command: (_ for _ in ()).throw(CommandError("lookup failed")),
    )
    assert (
        prepare_environment._initial_mode_state(
            "subscription",
            "environment",
            "",
            "",
        )
        == "unknown"
    )


def test_app_settings_preservation_is_derived_from_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_az_json(command: list[str]) -> list[dict[str, Any]]:
        commands.append(command)
        if command[:2] == ["group", "list"]:
            return [{"name": "proof-resource-group"}]
        return [
            {
                "name": "proof-app",
                "tags": {
                    "azd-service-name": "app",
                    "azd-env-name": "environment",
                },
            }
        ]

    monkeypatch.setattr(prepare_environment, "az_json", fake_az_json)
    assert (
        prepare_environment._preserve_existing_app_settings(
            "subscription",
            "environment",
            "",
            "",
        )
        == "true"
    )
    assert commands[1][commands[1].index("--resource-group") + 1] == "proof-resource-group"


def test_app_settings_preservation_fails_closed_for_ambiguous_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_environment,
        "az_json",
        lambda _command: [
            {
                "tags": {
                    "azd-service-name": "app",
                    "azd-env-name": "environment",
                }
            },
            {
                "tags": {
                    "azd-service-name": "app",
                    "azd-env-name": "environment",
                }
            },
        ],
    )
    with pytest.raises(CommandError, match="Multiple App Services"):
        prepare_environment._preserve_existing_app_settings(
            "subscription",
            "environment",
            "proof-resource-group",
            "",
        )


def test_app_settings_preservation_ignores_other_azd_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prepare_environment,
        "az_json",
        lambda _command: [
            {
                "tags": {
                    "azd-service-name": "app",
                    "azd-env-name": "another-environment",
                }
            }
        ],
    )
    assert (
        prepare_environment._preserve_existing_app_settings(
            "subscription",
            "environment",
            "shared-resource-group",
            "",
        )
        == "false"
    )


def test_authenticated_request_uses_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Request] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    def fake_urlopen(request: Request, *, timeout: float) -> Response:
        del timeout
        captured.append(request)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert verify_deployment._request_object(
        "https://example.test/health",
        token="opaque-token",
    ) == {"status": "ok"}
    assert captured[0].get_header("Authorization") == "Bearer opaque-token"


def test_permission_checks_actions_and_exclusions() -> None:
    permission = {
        "actions": ["Microsoft.Authorization/*"],
        "notActions": ["Microsoft.Authorization/roleAssignments/delete"],
    }
    assert preflight._permission_grants(
        permission,
        "Microsoft.Authorization/roleAssignments/write",
    )
    assert not preflight._permission_grants(
        permission,
        "Microsoft.Authorization/roleAssignments/delete",
    )


def test_networked_execution_requires_auth_and_bounded_ingress() -> None:
    with pytest.raises(CommandError, match="offline"):
        preflight._validate_execution_exposure("offline", "true", "true", "192.0.2.1/32")
    with pytest.raises(CommandError, match="operator allowlist"):
        preflight._validate_execution_exposure("entra", "true", "true", "")
    with pytest.raises(CommandError, match="bounded IPv4"):
        preflight._validate_execution_exposure("entra", "true", "true", "0.0.0.0/0")
    preflight._validate_execution_exposure(
        "entra",
        "true",
        "true",
        "192.0.2.1/32",
    )
    with pytest.raises(CommandError, match="Private-only App Service ingress"):
        preflight._validate_execution_exposure("entra", "false", "false", "")


def test_key_vault_mode_changes_require_confirmed_private_access() -> None:
    preflight._validate_key_vault_mode_access("entra", "fresh", "false")
    preflight._validate_key_vault_mode_access("entra", "clean", "false")
    preflight._validate_key_vault_mode_access("key", "clean", "true")
    preflight._validate_key_vault_mode_access("entra", "key", "true")
    with pytest.raises(CommandError, match="Key Vault data-plane access"):
        preflight._validate_key_vault_mode_access("key", "clean", "false")
    with pytest.raises(CommandError, match="Key Vault data-plane access"):
        preflight._validate_key_vault_mode_access("entra", "unknown", "false")


def test_deployment_manifest_ignores_caches_and_rejects_environment_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path
    (root / "src" / "package" / "__pycache__").mkdir(parents=True)
    (root / "benchmark_packs" / "example").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="ascii")
    (root / "requirements.lock").write_text("example==1\n", encoding="ascii")
    for relative in stage_package.ALLOWED_FILES[2:]:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="ascii")
    (root / "src" / "package" / "module.py").write_text("VALUE = 1\n", encoding="ascii")
    (root / "src" / "package" / "__pycache__" / "module.pyc").write_bytes(b"cache")
    monkeypatch.setattr(stage_package, "ROOT", root)

    manifest = stage_package.build_manifest()
    assert [entry["path"] for entry in manifest] == [
        "infra/__init__.py",
        "infra/scripts/__init__.py",
        "infra/scripts/release_runtime.py",
        "infra/scripts/runtime_probe.py",
        "pyproject.toml",
        "requirements.lock",
        "src/package/module.py",
    ]

    environment = root / "src" / "package" / ".env"
    environment.write_text("SECRET=not-a-real-secret\n", encoding="ascii")
    with pytest.raises(ValueError, match="Environment file"):
        stage_package.build_manifest()


def test_stale_secret_removal_uses_data_plane_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool = True) -> Any:
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(sync_auth_mode, "run", fake_run)
    sync_auth_mode._remove_secret_without_reading("example-vault", "deployment")

    assert len(commands) == 1
    assert commands[0][1:4] == ["keyvault", "secret", "delete"]
    assert commands[0][commands[0].index("--vault-name") + 1] == "example-vault"
    assert "value" not in commands[0]


def test_stale_secret_removal_surfaces_data_plane_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], *, check: bool = True) -> Any:
        del command, check
        return SimpleNamespace(returncode=1, stderr="Forbidden", stdout="")

    monkeypatch.setattr(sync_auth_mode, "run", fake_run)
    with pytest.raises(CommandError, match="private DNS"):
        sync_auth_mode._remove_secret_without_reading("example-vault", "deployment")


def test_role_creation_uses_actual_principal_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "/subscriptions/example/resourceGroups/rg"
    owned_name = sync_auth_mode._owned_assignment_name(
        "principal", scope, "Key Vault Secrets Officer"
    )
    assignment_checks = iter(
        [[], [{"id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{owned_name}"}]]
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        sync_auth_mode,
        "_role_assignments",
        lambda *_args: next(assignment_checks),
    )

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(sync_auth_mode, "run", fake_run)

    assert sync_auth_mode._ensure_role(
        "principal",
        "User",
        "/subscriptions/example/resourceGroups/rg",
        "Key Vault Secrets Officer",
    )
    assert commands[0][commands[0].index("--assignee-principal-type") + 1] == "User"


def test_failed_role_propagation_removes_the_created_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monotonic_values = iter([0.0, 181.0])
    monkeypatch.setattr(sync_auth_mode, "_role_assignments", lambda *_args: [])
    monkeypatch.setattr(
        "infra.scripts.sync_auth_mode.time.monotonic",
        lambda: next(monotonic_values),
    )

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(sync_auth_mode, "run", fake_run)

    with pytest.raises(CommandError, match="Timed out"):
        sync_auth_mode._ensure_role(
            "principal",
            "User",
            "/subscriptions/example/resourceGroups/rg",
            "Key Vault Secrets Officer",
        )

    assert any("create" in command for command in commands)
    create = next(command for command in commands if "create" in command)
    expected_name = sync_auth_mode._owned_assignment_name(
        "principal",
        "/subscriptions/example/resourceGroups/rg",
        "Key Vault Secrets Officer",
    )
    assert create[create.index("--name") + 1] == expected_name
    delete = next(command for command in commands if "delete" in command)
    assert delete[delete.index("--ids") + 1].endswith(f"/roleAssignments/{expected_name}")


def test_interrupted_role_propagation_removes_owned_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    checks = 0

    def fake_assignments(*_args: object) -> list[dict[str, str]]:
        nonlocal checks
        checks += 1
        if checks == 1:
            return []
        raise KeyboardInterrupt

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(sync_auth_mode, "_role_assignments", fake_assignments)
    monkeypatch.setattr(sync_auth_mode, "run", fake_run)
    monkeypatch.setattr("infra.scripts.sync_auth_mode.time.monotonic", lambda: 0.0)

    with pytest.raises(KeyboardInterrupt):
        sync_auth_mode._ensure_role(
            "principal",
            "User",
            "/subscriptions/example/resourceGroups/rg",
            "Key Vault Secrets Officer",
        )

    assert any("create" in command for command in commands)
    assert any("delete" in command for command in commands)


def test_retry_recognizes_only_the_deterministic_owned_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "/subscriptions/example/resourceGroups/rg"
    role = "Key Vault Secrets Officer"
    assignment_name = sync_auth_mode._owned_assignment_name("principal", scope, role)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        sync_auth_mode,
        "_role_assignments",
        lambda *_args: [
            {"id": (f"{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_name}")}
        ],
    )
    monkeypatch.setattr(
        sync_auth_mode,
        "run",
        lambda command, **_kwargs: commands.append(command),
    )

    assert sync_auth_mode._ensure_role("principal", "User", scope, role)
    assert commands == []


def test_mode_state_only_skips_cleanup_for_known_never_key_states() -> None:
    assert not sync_auth_mode._requires_secret_cleanup("fresh")
    assert not sync_auth_mode._requires_secret_cleanup("clean")
    assert sync_auth_mode._requires_secret_cleanup("key")
    assert sync_auth_mode._requires_secret_cleanup("key-pending")
    assert sync_auth_mode._requires_secret_cleanup("unknown")


def test_permission_lookup_uses_scoped_inherited_assignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_az_json(command: list[str]) -> list[dict[str, Any]]:
        commands.append(command)
        if command[:3] == ["role", "assignment", "list"]:
            return [
                {
                    "scope": "/providers/Microsoft.Management/managementGroups/tenant-root",
                    "roleDefinitionId": "role-id",
                }
            ]
        return [{"permissions": [{"actions": ["*"], "notActions": []}]}]

    monkeypatch.setattr(preflight, "az_json", fake_az_json)

    assert preflight._has_action(
        "principal",
        "User",
        "/subscriptions/target/resourceGroups/ai",
        "Microsoft.Authorization/roleAssignments/write",
    )
    assert all(command[command.index("--subscription") + 1] == "target" for command in commands)
    assert "--include-groups" in commands[0]
    assert commands[0][commands[0].index("--scope") + 1] == (
        "/subscriptions/target/resourceGroups/ai"
    )
    assert "--include-inherited" in commands[0]
    assert "--all" not in commands[0]
    assert commands[1][commands[1].index("--name") + 1] == "role-id"


def test_role_definition_name_accepts_full_resource_id() -> None:
    assert (
        preflight._role_definition_name(
            "/subscriptions/example/providers/Microsoft.Authorization/roleDefinitions/role-id"
        )
        == "role-id"
    )


def test_key_write_uses_a_temporary_file_and_removes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_paths: list[Path] = []
    captured_values: list[str] = []

    def fake_run(command: list[str], *, check: bool = True) -> Any:
        del check
        path = Path(command[command.index("--file") + 1])
        captured_paths.append(path)
        captured_values.append(path.read_text(encoding="utf-8"))
        assert "temporary-value" not in command
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(sync_auth_mode, "run", fake_run)
    sync_auth_mode._write_secret("example-vault", "temporary-value", "deployment")

    assert captured_values == ["temporary-value"]
    assert captured_paths and not captured_paths[0].exists()


def test_proof_run_requires_networked_solution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        verify_deployment,
        "_request_array",
        lambda _url: [
            {
                "definition": {"id": "example"},
                "solutions": [{"id": "offline", "networked": False}],
            }
        ],
    )
    with pytest.raises(CommandError, match="declared as networked"):
        verify_deployment._proof_run("https://example.test", "example", "offline")


def test_arm_verification_checks_private_services_roles_and_key_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[Any, ...]] = []

    def check(*args: Any, **kwargs: Any) -> str:
        received.append((*args, kwargs))
        return "principal"

    monkeypatch.setattr(verify_deployment, "verify_arm", check)
    assert (
        verify_deployment._verify_arm_state(
            "deployment",
            "rg",
            "app",
            "storage",
            "vault",
            "entra",
            "primary",
            mai_scope="secondary",
            public_demo=True,
            scm_cidr="192.0.2.1/32",
        )
        == "principal"
    )
    assert received[0][-2] == ("primary", "secondary")
    assert received[0][-1] == {
        "public_demo": True,
        "scm_cidr": "192.0.2.1/32",
        "expected_endpoint_ids": (),
    }


def test_proof_run_uses_api_contract_and_requires_evaluated_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None, dict[str, str] | None]] = []
    timeouts: list[float] = []
    run_id = "a" * 64
    evidence_payloads = {
        role: f"synthetic-{role}".encode()
        for role in ("input", "raw", "parsed", "comparison", "result")
    }
    evidence = [
        {
            "role": role,
            "artifact_id": hashlib.sha256(content).hexdigest(),
            "attempt_number": 1 if role != "input" else None,
        }
        for role, content in evidence_payloads.items()
    ]
    manifest = {
        "run_id": run_id,
        "networked_execution": True,
        "solution_ids": ["provider"],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    manifest_id = hashlib.sha256(manifest_bytes).hexdigest()
    report = {
        "run_id": run_id,
        "manifest_artifact_id": manifest_id,
        "results": [
            {
                "task_id": "task-1",
                "solution_id": "provider",
                "status": "succeeded",
                "metrics": [{"metric_id": "quality"}],
                "telemetry": {
                    "usage": {"dimensions": {"calls": 1}},
                    "provider_request_id": "provider-request-1",
                },
                "evidence": evidence,
            }
        ],
        "attempt_history": {
            "task-1": [
                {
                    "telemetry": {
                        "provider_request_id": "provider-request-1",
                    },
                    "evidence": [evidence[1]],
                }
            ]
        },
    }
    report_bytes = json.dumps(report, sort_keys=True).encode()
    report_id = hashlib.sha256(report_bytes).hexdigest()
    monkeypatch.setattr(
        verify_deployment,
        "_request_array",
        lambda _url: [
            {
                "definition": {"id": "example"},
                "solutions": [{"id": "provider", "networked": True}],
            }
        ],
    )

    def fake_request(
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        token: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 20,
    ) -> dict[str, Any]:
        del token
        timeouts.append(timeout)
        calls.append((url, method, payload, extra_headers))
        if method == "POST":
            return {"run_id": run_id}
        if url.endswith(f"/api/runs/{run_id}"):
            return {
                "status": "completed",
                "manifest_ref": f"/api/artifacts/{manifest_id}",
                "report_ref": f"/api/artifacts/{report_id}",
            }
        if url.endswith("/report"):
            return report
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(verify_deployment, "_request_object", fake_request)
    artifact_bytes = {
        manifest_id: manifest_bytes,
        report_id: report_bytes,
        **{hashlib.sha256(content).hexdigest(): content for content in evidence_payloads.values()},
    }
    monkeypatch.setattr(
        verify_deployment,
        "_request_bytes",
        lambda url, **_kwargs: artifact_bytes[url.rsplit("/", 1)[-1]],
    )
    verify_deployment._proof_run(
        "https://example.test",
        "example",
        "provider",
    )

    post = next(call for call in calls if call[1] == "POST")
    assert post[2] == {
        "benchmark_id": "example",
        "solution_ids": ["provider"],
        "retry_limit": 3,
        "allow_networked": True,
    }
    assert post[3] is not None
    assert post[3]["Idempotency-Key"].startswith("deployment-proof-")
    assert 620 in timeouts
