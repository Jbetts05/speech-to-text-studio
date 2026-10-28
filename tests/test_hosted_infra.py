from __future__ import annotations

import copy
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from infra.scripts import (
    hosted_checks,
    preflight,
    prepare_environment,
    preservation,
    runtime_probe,
    stage_package,
    sync_auth_mode,
    verify_deployment,
)
from infra.scripts._azure import CommandError

GROUP = "/subscriptions/example/resourceGroups/selected"
APP = f"{GROUP}/providers/Microsoft.Web/sites/app"
VNET = f"{GROUP}/providers/Microsoft.Network/virtualNetworks/app"
STORAGE = f"{GROUP}/providers/Microsoft.Storage/storageAccounts/app"
VAULT = f"{GROUP}/providers/Microsoft.KeyVault/vaults/app"
PROVIDERS = tuple(
    f"{GROUP}/providers/Microsoft.CognitiveServices/accounts/{name}"
    for name in ("primary", "secondary")
)


def topology() -> dict[str, Any]:
    settings = {
        "AI_BENCH_RUNTIME": "azure",
        "AI_BENCH_AUTH_MODE": "entra",
        "AI_BENCH_EXECUTION_BACKEND": "azure-table-blob",
        "AI_BENCH_STORAGE_BACKEND": "azure-table-blob",
        "AI_BENCH_AZURE_NAMESPACE": "application-v1",
        "AI_BENCH_PUBLIC_DEMO_MODE": "true",
        "AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS": "true",
        "AI_BENCH_PUBLIC_ORIGIN": "https://app.example.test",
        "AI_BENCH_AZURE_ARTIFACT_CONTAINER": "evidence",
        "AI_BENCH_AZURE_STAGING_CONTAINER": "staging",
        "AI_BENCH_AZURE_STATE_TABLE": "State",
        "AI_BENCH_SPEECH_ENDPOINT": "https://primary.example.test/",
        "AI_BENCH_OPENAI_ENDPOINT": "https://primary.example.test/",
        "AI_BENCH_MAI_ENDPOINT": "https://secondary.example.test/",
    }
    value: dict[str, Any] = {
        "app": {
            "id": APP,
            "defaultHostName": "app.example.test",
            "identity": {"principalId": "app-principal"},
            "httpsOnly": True,
            "publicNetworkAccess": "Enabled",
            "virtualNetworkSubnetId": f"{VNET}/subnets/app-service",
            "serverFarmId": f"{GROUP}/providers/Microsoft.Web/serverfarms/app",
        },
        "config": {
            "scmIpSecurityRestrictionsUseMain": False,
            "scmIpSecurityRestrictionsDefaultAction": "Deny",
            "scmMinTlsVersion": "1.2",
            "minTlsVersion": "1.2",
            "ftpsState": "Disabled",
            "vnetRouteAllEnabled": True,
            "ipSecurityRestrictionsDefaultAction": "Allow",
            "ipSecurityRestrictions": [],
            "scmIpSecurityRestrictions": [{"action": "Allow", "ipAddress": "192.0.2.1/32"}],
        },
        "storage": {
            "id": STORAGE,
            "publicNetworkAccess": "Disabled",
            "allowSharedKeyAccess": False,
            "allowBlobPublicAccess": False,
            "enableHttpsTrafficOnly": True,
            "minimumTlsVersion": "TLS1_2",
        },
        "vault": {
            "id": VAULT,
            "properties": {
                "publicNetworkAccess": "Disabled",
                "enableRbacAuthorization": True,
                "enablePurgeProtection": True,
                "enableSoftDelete": True,
            },
        },
        "settings": [{"name": key, "value": item} for key, item in settings.items()],
        "roles": [],
        "endpoints": [],
        "rest": {
            f"{GROUP}/providers/Microsoft.Web/serverfarms/app": {
                "properties": {"reserved": True},
                "sku": {"capacity": 1, "name": "B1"},
            },
            f"{APP}/config/authsettingsV2": {"properties": {"platform": {"enabled": False}}},
            f"{APP}/basicPublishingCredentialsPolicies/scm": {"properties": {"allow": False}},
            f"{APP}/basicPublishingCredentialsPolicies/ftp": {"properties": {"allow": False}},
            f"{STORAGE}/blobServices/default": {
                "properties": {
                    "deleteRetentionPolicy": {"enabled": False},
                    "containerDeleteRetentionPolicy": {"enabled": False},
                    "isVersioningEnabled": False,
                }
            },
            f"{STORAGE}/managementPolicies/default": {"properties": {"policy": {"rules": []}}},
            f"{STORAGE}/tableServices/default/tables/State": {"name": "State"},
        },
    }
    roles = [
        (f"{STORAGE}/blobServices/default/containers/{name}", hosted_checks.BLOB_ROLE)
        for name in ("evidence", "staging")
    ] + [
        (f"{STORAGE}/tableServices/default/tables/State", hosted_checks.TABLE_ROLE),
        *((scope, hosted_checks.AI_ROLE) for scope in PROVIDERS),
    ]
    for scope, name in zip(PROVIDERS, ("primary", "secondary"), strict=True):
        value["rest"][scope] = {
            "kind": "AIServices",
            "sku": {"name": "S0"},
            "properties": {
                "endpoint": f"https://{name}.example.test/",
                "disableLocalAuth": True,
                "publicNetworkAccess": "Enabled",
                "networkAcls": {"defaultAction": "Allow", "ipRules": []},
            },
        }
    for scope, role in roles:
        value["roles"].append(
            {
                "principalId": "app-principal",
                "scope": scope,
                "roleDefinitionId": (
                    "/subscriptions/example/providers/Microsoft.Authorization/"
                    f"roleDefinitions/{role}"
                ),
            }
        )
        if "/containers/" in scope:
            value["rest"][scope] = {"properties": {"publicAccess": "None"}}
    targets = [
        (STORAGE, "blob", {"privatelink.blob.core.windows.net"}),
        (STORAGE, "table", {"privatelink.table.core.windows.net"}),
        (VAULT, "vault", {"privatelink.vaultcore.azure.net"}),
    ]
    for index, (scope, kind, zones) in enumerate(targets):
        identity = f"{GROUP}/providers/Microsoft.Network/privateEndpoints/pe{index}"
        value["endpoints"].append(
            {
                "id": identity,
                "provisioningState": "Succeeded",
                "subnet": {"id": f"{VNET}/subnets/private-endpoints"},
                "networkInterfaces": [
                    {"id": f"{GROUP}/providers/Microsoft.Network/networkInterfaces/nic{index}"}
                ],
                "privateLinkServiceConnections": [
                    {
                        "privateLinkServiceId": scope,
                        "groupIds": [kind],
                        "privateLinkServiceConnectionState": {"status": "Approved"},
                    }
                ],
            }
        )
        configs = []
        for zone in sorted(zones):
            zone_id = f"{GROUP}/providers/Microsoft.Network/privateDnsZones/{zone}"
            configs.append({"properties": {"privateDnsZoneId": zone_id}})
            value["rest"][f"{zone_id}/virtualNetworkLinks"] = {
                "value": [
                    {
                        "properties": {
                            "virtualNetwork": {"id": VNET},
                            "provisioningState": "Succeeded",
                            "registrationEnabled": False,
                        }
                    }
                ]
            }
        value["rest"][f"{identity}/privateDnsZoneGroups"] = {
            "value": [{"properties": {"privateDnsZoneConfigs": configs}}],
        }
    return value


def verify(value: dict[str, Any]) -> str:
    def read(command: list[str]) -> Any:
        assert not {"create", "delete", "set", "restart"} & set(command)
        if command[0] == "rest":
            path = (
                command[command.index("--url") + 1]
                .split("management.azure.com", 1)[1]
                .split("?")[0]
            )
            return value["rest"][path]
        if command[:2] == ["webapp", "show"]:
            return value["app"]
        if command[:3] == ["webapp", "config", "show"]:
            return value["config"]
        if command[:3] == ["webapp", "config", "appsettings"]:
            return value["settings"]
        if command[:2] == ["storage", "account"]:
            return value["storage"]
        if command[:2] == ["keyvault", "show"]:
            return value["vault"]
        if command[:2] == ["network", "private-endpoint"]:
            return value["endpoints"]
        if command[:2] == ["role", "assignment"]:
            return value["roles"]
        raise AssertionError(command)

    return hosted_checks.verify_arm(
        read,
        "example",
        "selected",
        "app",
        "app",
        "app",
        "entra",
        PROVIDERS,
        public_demo=True,
        scm_cidr="192.0.2.1/32",
        expected_endpoint_ids=tuple(
            f"{GROUP}/providers/Microsoft.Network/privateEndpoints/pe{index}" for index in range(3)
        ),
    )


def test_private_storage_public_providers_with_exact_roles() -> None:
    assert verify(topology()) == "app-principal"


@pytest.mark.parametrize(
    "mutation",
    [
        "role-name-only",
        "broad-role",
        "wrong-principal",
        "extra-vault-role",
        "pending-pe",
        "wrong-subnet",
        "no-zone",
        "no-link",
        "soft-delete",
        "versioning",
        "scm-inherits",
        "scm-public",
        "main-auth",
        "missing-vault",
        "shared-key",
        "key-setting",
        "lifecycle",
        "public-origin",
        "provider-ip-restriction",
        "provider-local-auth",
        "provider-json",
    ],
)
def test_verifier_rejects_incomplete_or_broadened_release(mutation: str) -> None:
    value = topology()
    if mutation == "role-name-only":
        value["roles"][0] = {"roleDefinitionName": "Storage Blob Data Contributor"}
    elif mutation == "broad-role":
        value["roles"][0]["scope"] = STORAGE
    elif mutation == "wrong-principal":
        value["roles"][0]["principalId"] = "operator"
    elif mutation == "extra-vault-role":
        value["roles"].append(
            {
                "scope": VAULT,
                "principalId": "app-principal",
                "roleDefinitionId": hosted_checks.VAULT_ROLE,
            }
        )
    elif mutation == "pending-pe":
        value["endpoints"][0]["privateLinkServiceConnections"][0][
            "privateLinkServiceConnectionState"
        ]["status"] = "Pending"
    elif mutation == "wrong-subnet":
        value["endpoints"][0]["subnet"]["id"] = f"{VNET}/subnets/other"
    elif mutation == "no-zone":
        value["rest"][f"{value['endpoints'][-1]['id']}/privateDnsZoneGroups"]["value"] = []
    elif mutation == "no-link":
        key = next(key for key in value["rest"] if key.endswith("/virtualNetworkLinks"))
        value["rest"][key]["value"] = []
    elif mutation == "soft-delete":
        value["rest"][f"{STORAGE}/blobServices/default"]["properties"]["deleteRetentionPolicy"][
            "enabled"
        ] = True
    elif mutation == "versioning":
        value["rest"][f"{STORAGE}/blobServices/default"]["properties"]["isVersioningEnabled"] = True
    elif mutation == "scm-inherits":
        value["config"]["scmIpSecurityRestrictionsUseMain"] = True
    elif mutation == "scm-public":
        value["config"]["scmIpSecurityRestrictionsDefaultAction"] = "Allow"
    elif mutation == "main-auth":
        value["rest"][f"{APP}/config/authsettingsV2"]["properties"]["platform"]["enabled"] = True
    elif mutation == "missing-vault":
        value["endpoints"].pop()
    elif mutation == "shared-key":
        value["storage"]["allowSharedKeyAccess"] = True
    elif mutation == "key-setting":
        value["settings"].append({"name": "AI_BENCH_FOUNDRY_API_KEY", "value": "synthetic"})
    elif mutation == "lifecycle":
        value["rest"][f"{STORAGE}/managementPolicies/default"] = {
            "properties": {"policy": {"rules": [{"enabled": True}]}}
        }
    elif mutation == "public-origin":
        next(
            setting for setting in value["settings"] if setting["name"] == "AI_BENCH_PUBLIC_ORIGIN"
        )["value"] = "https://untrusted.example.test/path"
    elif mutation == "provider-ip-restriction":
        value["rest"][PROVIDERS[0]]["properties"]["networkAcls"]["defaultAction"] = "Deny"
    elif mutation == "provider-local-auth":
        value["rest"][PROVIDERS[0]]["properties"]["disableLocalAuth"] = False
    elif mutation == "provider-json":
        value["settings"].append(
            {
                "name": "AI_BENCH_SPEECH_PROVIDER_CONFIG",
                "value": "synthetic-config.json",
            }
        )
    with pytest.raises(CommandError):
        verify(value)


def test_public_exception_is_explicit_and_scm_can_use_entra_only() -> None:
    preflight._validate_execution_exposure("entra", "true", "true", "", "true", "192.0.2.1/32")
    preflight._validate_execution_exposure("entra", "true", "true", "", "true", "")
    for auth, main, scm in (
        ("key", "", "192.0.2.1/32"),
        ("entra", "", None),
        ("entra", "", "0.0.0.0/0"),
    ):
        with pytest.raises(CommandError):
            preflight._validate_execution_exposure(auth, "true", "true", main, "true", scm)
    with pytest.raises(CommandError, match="operator allowlist"):
        preflight._validate_execution_exposure("entra", "true", "true", "", "false", "192.0.2.1/32")


def test_fresh_vault_requires_exact_absence_not_group_tag_or_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for error in ("Forbidden", "(ResourceGroupNotFound) unknown group", "network timeout"):
        monkeypatch.setattr(
            prepare_environment,
            "az_json",
            lambda _, message=error: (_ for _ in ()).throw(CommandError(message)),
        )
        assert (
            prepare_environment._initial_mode_state("s", "env", "tagged-existing", "", "new-target")
            == "unknown"
        )

    def deleted(command: list[str]) -> Any:
        if command[:2] == ["keyvault", "show"]:
            raise CommandError("(ResourceNotFound) absent")
        return [{"name": "new-target"}]

    monkeypatch.setattr(prepare_environment, "az_json", deleted)
    assert (
        prepare_environment._initial_mode_state("s", "env", "tagged-existing", "", "new-target")
        == "unknown"
    )


def test_mode_cleanup_preserves_unowned_role_assignments(monkeypatch: pytest.MonkeyPatch) -> None:
    own = sync_auth_mode._owned_assignment_name("app", VAULT, sync_auth_mode.KEY_VAULT_SECRETS_USER)
    assignments = [
        {"id": f"{VAULT}/providers/Microsoft.Authorization/roleAssignments/{name}"}
        for name in ("existing-operator-owned", own)
    ]
    commands: list[list[str]] = []
    monkeypatch.setattr(sync_auth_mode, "_role_assignments", lambda *_: assignments)
    monkeypatch.setattr(sync_auth_mode, "run", lambda command: commands.append(command))
    sync_auth_mode._delete_role("app", VAULT, sync_auth_mode.KEY_VAULT_SECRETS_USER)
    assert len(commands) == 1 and commands[0][commands[0].index("--ids") + 1].endswith(own)


def test_preservation_detects_model_tags_and_operator_role_changes() -> None:
    before = {
        "schema_version": "hosted-preservation-v1",
        "group_id": GROUP,
        "group_tags": {"owner": "keep"},
        "accounts": {"account": {"model": "immutable"}},
        "role_assignments": {"existing-role": {"scope": GROUP}},
    }
    preservation.compare(before, copy.deepcopy(before))
    for field in ("group_tags", "accounts", "role_assignments"):
        changed = copy.deepcopy(before)
        changed[field] = {}
        with pytest.raises(CommandError):
            preservation.compare(before, changed)


def test_ordinary_verification_cannot_implicitly_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["verify", "--dataset-id", "selected"])
    monkeypatch.setattr(verify_deployment, "az_json", lambda _: pytest.fail("Must not contact ARM"))
    with pytest.raises(CommandError, match="explicit --require-proof"):
        verify_deployment.main()


def test_runtime_probe_refuses_scm_and_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUDU_BUILD_VERSION", "synthetic")
    with pytest.raises(ValueError, match="never the SCM"):
        runtime_probe.worker_identity(1)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("203.0.113.1", 443))],
    )
    with pytest.raises(ValueError, match="exclusively to private"):
        runtime_probe.private_connectivity("https://synthetic.example.test")


def test_rebuild_rejects_generated_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    static = tmp_path / "src" / "ai_bench" / "web" / "static"
    static.mkdir(parents=True)
    index = static / "index.html"
    index.write_text("reviewed")
    monkeypatch.setattr(stage_package, "ROOT", tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: index.write_text("changed"))
    with pytest.raises(ValueError, match="review and commit"):
        stage_package.rebuild_frontend()


def test_attestations_and_payloads_are_not_added_to_staging_allowlist() -> None:
    assert all(not name.endswith(".json") for name in stage_package.ALLOWED_FILES)
    assert "uploads" in stage_package.FORBIDDEN_PARTS
    assert (
        json.loads(Path("infra/main.parameters.json").read_text())["parameters"]["publicDemoMode"][
            "value"
        ]
        == "${AI_BENCH_PUBLIC_DEMO_MODE}"
    )


@pytest.mark.parametrize("failure", [None, "matrix", "attempts", "retention"])
def test_public_proof_is_exactly_two_by_five_and_ten_days(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    solutions = [f"provider-{number}" for number in range(5)]
    dataset = {
        "id": "synthetic",
        "benchmark_id": "example",
        "sample_count": 2,
        "scored_sample_count": 2,
        "pinned": False,
        "published_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-11T00:00:00+00:00",
        "revision": {"content_hash": "a" * 64},
    }
    manifest = {
        "run_id": "run",
        "solution_ids": solutions,
        "networked_execution": True,
        "dataset_revision": dataset["revision"],
        "retry_limit": 3,
        "sample_ids": ["one", "two"],
        "sample_snapshot_artifact_id": "d" * 64,
    }
    results = [
        {
            "task_id": f"{solution}-{sample}",
            "solution_id": solution,
            "status": "succeeded",
            "metrics": [{"metric_id": "quality"}],
            "telemetry": {"provider_request_id": "observed"},
            "evidence": [
                {"role": role, "artifact_id": str(sample + 1) * 64}
                for role in ("input", "raw", "parsed", "comparison", "result")
            ],
        }
        for solution in solutions
        for sample in range(2)
    ]
    report: dict[str, Any] = {
        "run_id": "run",
        "manifest_artifact_id": "b" * 64,
        "results": results,
        "attempt_history": {
            result["task_id"]: [
                {
                    "telemetry": {"provider_request_id": "observed"},
                    "evidence": result["evidence"],
                }
            ]
            for result in results
        },
    }
    if failure == "matrix":
        report["results"] = results[:-1]
    elif failure == "attempts":
        report["attempt_history"][results[0]["task_id"]] *= 4
    elif failure == "retention":
        dataset["expires_at"] = "2026-01-31T00:00:00+00:00"
    posts: list[dict[str, Any]] = []

    def request(url: str, **options: Any) -> Any:
        if options.get("method") == "POST":
            posts.append(options)
            return {"run_id": "run"}
        if url.endswith("/datasets/synthetic"):
            return dataset
        if url.endswith("/api/runtime"):
            return {"server_time": "2026-01-01T00:05:00+00:00"}
        if url.endswith("/report"):
            return report
        return {
            "status": "completed",
            "manifest_ref": f"/api/artifacts/{'b' * 64}",
            "report_ref": f"/api/artifacts/{'c' * 64}",
            "dataset_id": "synthetic",
            "expires_at": dataset["expires_at"],
            "pinned": False,
        }

    monkeypatch.setattr(verify_deployment, "_request_object", request)
    monkeypatch.setattr(
        verify_deployment,
        "_request_array",
        lambda _: [
            {
                "definition": {"id": "example"},
                "solutions": [{"id": solution, "networked": True} for solution in solutions],
            }
        ],
    )
    monkeypatch.setattr(
        verify_deployment,
        "_verified_artifact_object",
        lambda _, ref: manifest if ref.endswith("b" * 64) else report,
    )
    monkeypatch.setattr(verify_deployment, "_verified_artifact_bytes", lambda *_: b"synthetic")
    audio: list[dict[str, Any]] = []
    monkeypatch.setattr(
        verify_deployment, "_verify_audio_delivery", lambda _, item: audio.append(item)
    )
    if failure:
        with pytest.raises(CommandError):
            verify_deployment._proof_run(
                "https://example.test", "example", solutions, dataset_id="synthetic"
            )
    else:
        verify_deployment._proof_run(
            "https://example.test", "example", solutions, dataset_id="synthetic"
        )
        assert posts[0]["payload"]["solution_ids"] == solutions
        assert posts[0]["payload"]["retry_limit"] == 3
        assert posts[0]["extra_headers"]["X-AI-Bench-Intent"] == "public-demo"
        assert posts[0]["extra_headers"]["Idempotency-Key"].startswith("v1.")
        assert len(audio) == 2


def test_capacity_is_checked_without_treating_unknown_as_unlimited() -> None:
    records = [{"name": {"value": "PrivateEndpoints"}, "currentValue": 5, "limit": 10}]
    preflight._capacity_available(records, {"privateendpoints"}, 5, "PE")
    with pytest.raises(CommandError, match="Insufficient"):
        preflight._capacity_available(records, {"privateendpoints"}, 6, "PE")
    with pytest.raises(CommandError, match="explicit capacity"):
        preflight._capacity_available([], {"privateendpoints"}, 5, "PE")


def test_app_service_wildcard_quota_uses_only_the_known_regional_vm_label() -> None:
    records = [
        {
            "name": {"value": "*", "localizedValue": "Total Regional VMs"},
            "currentValue": 10,
            "limit": 11,
        }
    ]
    preflight._capacity_available(records, {"totalregionalvms"}, 1, "App Service")
    with pytest.raises(CommandError, match="Insufficient"):
        preflight._capacity_available(records, {"totalregionalvms"}, 2, "App Service")
    with pytest.raises(CommandError, match="explicit capacity"):
        preflight._capacity_available(records, {"storageaccounts"}, 1, "Storage")
    with pytest.raises(CommandError, match="explicit capacity"):
        preflight._capacity_available(records * 2, {"totalregionalvms"}, 1, "App Service")


def test_regional_capacity_uses_stable_web_total_vm_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def quotas(arguments: list[str]) -> dict[str, object]:
        url = arguments[arguments.index("--url") + 1]
        if "/Microsoft.Web/" in url:
            assert url.endswith("api-version=2024-11-01")
            return {
                "value": [
                    {
                        "name": {"value": "*", "localizedValue": "Total Regional VMs"},
                        "currentValue": 0,
                        "limit": 11,
                    }
                ]
            }
        names = (
            ("StorageAccounts",)
            if "/Microsoft.Storage/" in url
            else ("VirtualNetworks", "PrivateEndpoints", "NetworkInterfaces")
        )
        return {
            "value": [{"name": {"value": name}, "currentValue": 1, "limit": 20} for name in names]
        }

    monkeypatch.setattr(preflight, "az_json", quotas)
    preflight._regional_capacity("synthetic-subscription", "swedencentral")


def test_storage_recovery_policy_cannot_be_changed_on_unowned_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "az_json", lambda _: {"id": STORAGE, "tags": {"owner": "other"}})
    with pytest.raises(CommandError, match="not created for this release"):
        preflight._validate_storage_target("example", "selected", "validname")


def test_deployed_optional_policy_is_preserved_and_unsupported_ingress_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: dict[str, Any] = {
        "ipSecurityRestrictions": [{"action": "Allow", "ipAddress": "192.0.2.0/24"}],
        "ipSecurityRestrictionsDefaultAction": "Deny",
        "scmIpSecurityRestrictions": [{"action": "Allow", "ipAddress": "192.0.2.1/32"}],
        "scmIpSecurityRestrictionsDefaultAction": "Deny",
    }

    def read(command: list[str]) -> Any:
        if command[0] == "resource":
            return [
                {"name": "app", "tags": {"azd-env-name": "selected", "azd-service-name": "app"}}
            ]
        if command[:3] == ["webapp", "config", "appsettings"]:
            return [
                {"name": "AI_BENCH_PUBLIC_DEMO_MODE", "value": "true"},
                {"name": "AI_BENCH_GPT_TRANSCRIBE_DEPLOYMENT", "value": "reviewed-deployment"},
                {"name": "AI_BENCH_KEY_VAULT_NAME", "value": "existing-vault"},
            ]
        if command[:2] == ["webapp", "show"]:
            return {"serverFarmId": APP + "-plan", "publicNetworkAccess": "Enabled"}
        if command[0] == "rest":
            return {"sku": {"name": "B1"}}
        return config

    monkeypatch.setattr(prepare_environment, "az_json", read)
    monkeypatch.setattr(prepare_environment, "azd_value", lambda *_args, **_kwargs: None)
    preserved = prepare_environment._deployed_optional_values("example", "selected", "selected")
    assert preserved["AI_BENCH_PUBLIC_DEMO_MODE"] == "true"
    assert preserved["AI_BENCH_GPT_TRANSCRIBE_DEPLOYMENT"] == "reviewed-deployment"
    assert preserved["AI_BENCH_INGRESS_ALLOWED_CIDR"] == "192.0.2.0/24"
    assert preserved["AI_BENCH_SCM_ALLOWED_CIDR"] == "192.0.2.1/32"
    config["ipSecurityRestrictions"] = []
    with pytest.raises(CommandError, match="default-deny"):
        prepare_environment._deployed_optional_values("example", "selected", "selected")
    monkeypatch.setattr(
        prepare_environment,
        "azd_value",
        lambda name, **_kwargs: "" if name == "AI_BENCH_INGRESS_ALLOWED_CIDR" else None,
    )
    explicitly_reset = prepare_environment._deployed_optional_values(
        "example", "selected", "selected"
    )
    assert "AI_BENCH_INGRESS_ALLOWED_CIDR" not in explicitly_reset


def test_unrelated_private_endpoint_is_not_mistaken_for_operation_owned_endpoint() -> None:
    value = topology()
    other = copy.deepcopy(value["endpoints"][0])
    other["id"] += "-another-operation"
    other["subnet"]["id"] = (
        f"{GROUP}/providers/Microsoft.Network/virtualNetworks/old/subnets/private"
    )
    value["endpoints"].insert(0, other)
    assert verify(value) == "app-principal"


def test_paid_proof_key_is_persisted_before_submission_and_reused(tmp_path: Path) -> None:
    state = tmp_path / "proof-state.json"
    payload = {"dataset_id": "synthetic", "solution_ids": ["one", "two"]}
    first_key = "v1.1767225600.00000000-0000-4000-8000-000000000001"
    assert verify_deployment._proof_key(state, payload, first_key) == first_key
    assert verify_deployment._proof_key(state, payload, "ignored-new-key") == first_key
    with pytest.raises(CommandError, match="exact dataset/solution"):
        verify_deployment._proof_key(state, {"dataset_id": "different"}, first_key)


def test_bootstrap_is_read_only_and_does_not_claim_readiness_or_proof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    values = {
        "AZURE_SUBSCRIPTION_ID": "example",
        "AZURE_RESOURCE_GROUP": "selected",
        "AI_BENCH_APP_NAME": "app",
        "AI_BENCH_APP_URL": "https://app.example.test",
        "AI_BENCH_STORAGE_ACCOUNT": "storage",
        "AI_BENCH_KEY_VAULT_NAME": "vault",
        "AI_BENCH_AUTH_MODE": "entra",
        "AI_BENCH_FOUNDRY_SCOPE": PROVIDERS[0],
        "AI_BENCH_MAI_SCOPE": PROVIDERS[1],
        "AI_BENCH_PUBLIC_DEMO_MODE": "true",
        "AI_BENCH_PRIVATE_ENDPOINT_IDS": '["blob","table","vault"]',
        "AI_BENCH_SCM_ALLOWED_CIDR": "192.0.2.1/32",
    }
    monkeypatch.setattr(sys, "argv", ["verify", "--bootstrap"])
    monkeypatch.setattr(verify_deployment, "require_command", lambda _: None)
    monkeypatch.setattr(verify_deployment, "azd_value", lambda name, **_: values.get(name))
    monkeypatch.setattr(
        verify_deployment, "_verify_arm_state", lambda *_args, **_kwargs: "principal"
    )
    monkeypatch.setattr(verify_deployment, "_verify_kudu", lambda *_args: None)
    urls: list[str] = []

    def wait_endpoint(url: str, *_args: object) -> dict[str, str]:
        urls.append(url)
        return {"status": "ok"}

    monkeypatch.setattr(
        verify_deployment,
        "_wait_endpoint",
        wait_endpoint,
    )
    monkeypatch.setattr(
        verify_deployment,
        "_request_object",
        lambda url: {
            "runtime": "azure",
            "execution_backend": "azure-table-blob",
            "public_demo": True,
        },
    )
    monkeypatch.setattr(
        verify_deployment, "_proof_run", lambda *_args, **_kwargs: pytest.fail("No paid proof")
    )
    assert verify_deployment.main() == 0
    assert urls == ["https://app.example.test/health"]
    output = capsys.readouterr().out
    assert "NOT deployment proof" in output and "not verified by this check" in output


def test_bootstrap_cannot_be_combined_with_provider_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["verify", "--bootstrap", "--require-proof"])
    monkeypatch.setattr(
        verify_deployment, "require_command", lambda _: pytest.fail("No Azure calls")
    )
    with pytest.raises(CommandError, match="cannot launch paid proof"):
        verify_deployment.main()


def test_preflight_rejects_hosted_json_fallback_without_reading_or_deleting_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def read(command: list[str]) -> list[str]:
        commands.append(command)
        return ["AI_BENCH_SPEECH_PROVIDER_CONFIG"]

    monkeypatch.setattr(preflight, "az_json", read)
    with pytest.raises(CommandError, match="never deletes"):
        preflight._validate_hosted_provider_settings("example", "selected", "app")
    assert len(commands) == 1
    assert commands[0][-1] == "[?name=='AI_BENCH_SPEECH_PROVIDER_CONFIG'].name"
    assert "delete" not in commands[0]
