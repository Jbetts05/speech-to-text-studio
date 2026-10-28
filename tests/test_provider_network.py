from __future__ import annotations

import copy
import socket
from pathlib import Path
from typing import Any

import pytest

from infra.scripts import preflight, preservation, provider_network, runtime_probe
from infra.scripts._azure import CommandError

GROUP = "/subscriptions/example/resourceGroups/selected"
SCOPES = {
    profile: f"{GROUP}/providers/Microsoft.CognitiveServices/accounts/{profile}"
    for profile in ("primary", "secondary")
}


def account(profile: str) -> dict[str, Any]:
    return {
        "id": SCOPES[profile],
        "name": profile,
        "type": "Microsoft.CognitiveServices/accounts",
        "resourceGroup": "selected",
        "location": "swedencentral" if profile == "primary" else "eastus",
        "kind": "AIServices",
        "sku": {"name": "S0", "tier": "Standard"},
        "tags": {"keep": "unchanged", "another-owner": "preserved"},
        "identity": {
            "type": "SystemAssigned, UserAssigned",
            "principalId": "existing-principal",
            "tenantId": "existing-tenant",
            "userAssignedIdentities": {
                f"{GROUP}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/original": {
                    "clientId": "existing-client",
                    "principalId": "existing-user-principal",
                },
            },
        },
        "properties": {
            "provisioningState": "Succeeded",
            "endpoint": f"https://{profile}.example.test/",
            "customSubDomainName": profile,
            "disableLocalAuth": True,
            "publicNetworkAccess": "Enabled",
            "networkAcls": {
                "defaultAction": "Deny",
                "ipRules": [{"value": "192.0.2.1"}],
                "bypass": "AzureServices",
                "virtualNetworkRules": [
                    {"id": f"{GROUP}/providers/Microsoft.Network/virtualNetworks/old/subnets/keep"}
                ],
            },
            "encryption": {
                "keySource": "Microsoft.KeyVault",
                "keyVaultProperties": {
                    "keyVaultUri": "https://original.example.test",
                    "keyName": "original-name",
                    "keyVersion": "original-version",
                },
            },
            "allowProjectManagement": True,
            "defaultProject": "original-project",
            "associatedProjects": ["original-project"],
            "dynamicThrottlingEnabled": False,
            "restrictOutboundNetworkAccess": True,
            "allowedFqdnList": ["approved.example.test"],
            "userOwnedStorage": [
                {"resourceId": f"{GROUP}/providers/Microsoft.Storage/storageAccounts/old"}
            ],
            "networkInjections": [{"scenario": "agent", "useMicrosoftManagedNetwork": True}],
            "privateEndpointConnections": [],
            "capabilities": [],
            "armFeatures": ["Microsoft.CognitiveServices/SyntheticCapability"],
        },
    }


def snapshot() -> dict[str, Any]:
    return {
        "schema_version": provider_network.SCHEMA,
        "accounts": {
            profile: {
                "observed": provider_network.observed_account(account(profile)),
                "writable": provider_network.writable_account(account(profile)),
            }
            for profile in SCOPES
        },
    }


def what_if() -> dict[str, Any]:
    return {
        "status": "Succeeded",
        "changes": [
            {
                "resourceId": scope,
                "changeType": "Modify",
                "before": account(profile),
                "after": provider_network.public_network(account(profile)),
            }
            for profile, scope in SCOPES.items()
        ],
    }


def test_writable_round_trip_changes_only_approved_network_fields() -> None:
    original = account("primary")
    writable = provider_network.writable_account(original)
    expected = copy.deepcopy(writable)
    expected["properties"]["networkAcls"]["defaultAction"] = "Allow"
    expected["properties"]["networkAcls"]["ipRules"] = []
    expected["properties"]["publicNetworkAccess"] = "Enabled"
    assert provider_network.public_network(writable) == expected
    for field in ("tags", "kind", "location", "sku"):
        assert expected[field] == original[field]
    for field in (
        "encryption",
        "customSubDomainName",
        "disableLocalAuth",
        "defaultProject",
        "associatedProjects",
        "allowProjectManagement",
        "userOwnedStorage",
        "networkInjections",
        "restrictOutboundNetworkAccess",
        "allowedFqdnList",
    ):
        assert expected["properties"][field] == original["properties"][field]
    assert "principalId" not in writable["identity"]
    assert list(writable["identity"]["userAssignedIdentities"].values()) == [{}]
    assert "endpoint" not in writable["properties"]
    assert "privateEndpointConnections" not in writable["properties"]
    assert "resourceGroup" not in writable
    assert writable["properties"]["armFeatures"] == original["properties"]["armFeatures"]
    assert provider_network.observed_account(original)["properties"]["armFeatures"] == [
        "Microsoft.CognitiveServices/SyntheticCapability"
    ]
    assert original["properties"]["networkAcls"]["defaultAction"] == "Deny"


@pytest.mark.parametrize("features", [None, {}, "feature", ["unreviewed-provider/feature"]])
def test_unrecognized_feature_metadata_fails_closed(features: object) -> None:
    original = account("primary")
    original["properties"]["armFeatures"] = features
    with pytest.raises(CommandError, match="feature-registration metadata"):
        provider_network.writable_account(original)


def test_feature_registration_metadata_stays_in_snapshot_and_drift_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = snapshot()
    current = {scope: account(profile) for profile, scope in SCOPES.items()}
    monkeypatch.setattr(provider_network, "read_account", lambda scope: current[scope])
    provider_network.validate_snapshot(frozen, SCOPES)
    current[SCOPES["primary"]]["properties"]["armFeatures"].append(
        "Microsoft.CognitiveServices/ChangedCapability"
    )
    with pytest.raises(CommandError, match="drifted"):
        provider_network.validate_snapshot(frozen, SCOPES)
    result = what_if()
    result["changes"][0]["after"]["properties"]["armFeatures"] = []
    with pytest.raises(CommandError, match="feature registrations"):
        provider_network.validate_what_if(result, frozen)
    result = what_if()
    for change in result["changes"]:
        change["after"]["properties"].pop("armFeatures")
    with pytest.raises(CommandError, match="network-only"):
        provider_network.validate_what_if(result, frozen)


def test_preview_normalizes_only_empty_acl_lists_and_null_api_properties() -> None:
    frozen = snapshot()
    result = what_if()
    for profile, change in zip(SCOPES, result["changes"], strict=True):
        observed = frozen["accounts"][profile]["observed"]
        observed["properties"]["networkAcls"]["virtualNetworkRules"] = []
        observed["properties"]["apiProperties"] = {"qnaAzureSearchEndpointKey": None}
        frozen["accounts"][profile]["writable"] = provider_network.writable_account(observed)
        for payload in (change["before"], change["after"]):
            payload["properties"]["networkAcls"].pop("virtualNetworkRules")
        change["after"]["properties"]["networkAcls"].pop("ipRules")
    provider_network.validate_what_if(result, frozen)
    result["changes"][0]["after"]["properties"]["networkAcls"]["virtualNetworkRules"] = [
        {"id": "unapproved-network"}
    ]
    with pytest.raises(CommandError, match="network-only"):
        provider_network.validate_what_if(result, frozen)


def test_unknown_writable_properties_and_secret_values_fail_closed() -> None:
    original = account("primary")
    original["properties"]["futureUnreviewedProperty"] = {"must": "not disappear"}
    with pytest.raises(CommandError, match="cannot be discarded"):
        provider_network.writable_account(original)
    original = account("primary")
    original["properties"]["apiProperties"] = {"qnaAzureSearchEndpointKey": "synthetic"}
    with pytest.raises(CommandError, match="secret-bearing"):
        provider_network.writable_account(original)


def test_frozen_snapshot_allows_only_original_or_approved_network_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {scope: account(profile) for profile, scope in SCOPES.items()}
    monkeypatch.setattr(provider_network, "read_account", lambda scope: current[scope])
    frozen = snapshot()
    provider_network.validate_snapshot(frozen, SCOPES)
    current[SCOPES["primary"]] = provider_network.public_network(current[SCOPES["primary"]])
    provider_network.validate_snapshot(frozen, SCOPES)
    current[SCOPES["primary"]]["tags"]["added-by-other-operator"] = "must-preserve"
    with pytest.raises(CommandError, match="drifted"):
        provider_network.validate_snapshot(frozen, SCOPES)


def test_missing_existing_account_never_falls_back_to_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_network,
        "az_json",
        lambda _: (_ for _ in ()).throw(
            CommandError("(ResourceNotFound) exact existing account is absent")
        ),
    )
    monkeypatch.setattr(
        provider_network, "run", lambda _: pytest.fail("Must not write config or mutate")
    )
    with pytest.raises(CommandError, match="ResourceNotFound"):
        provider_network.snapshot_accounts(SCOPES)


def test_prepare_freezes_actual_readback_and_never_mutates_azure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    stored = {provider_network.ENABLE_ENV: "true", provider_network.SNAPSHOT_ENV: "e30="}
    monkeypatch.setattr(provider_network, "selected_scopes", lambda: SCOPES)
    monkeypatch.setattr(
        provider_network, "read_account", lambda scope: account(scope.rsplit("/", 1)[-1])
    )
    monkeypatch.setattr(
        provider_network,
        "azd_value",
        lambda name, **_: stored[name],
    )

    def set_from_file(command: list[str]) -> None:
        commands.append(command)
        assert command[:4] == ["azd", "env", "set", "--file"]
        key, encoded = Path(command[4]).read_text(encoding="utf-8").strip().split("=", 1)
        stored[key] = encoded

    monkeypatch.setattr(provider_network, "run", set_from_file)
    provider_network.prepare_snapshot()
    assert len(commands) == 1
    assert provider_network.decode_snapshot(stored[provider_network.SNAPSHOT_ENV]) == snapshot()
    assert not Path(commands[0][4]).exists()


def test_snapshot_file_escapes_expansion_and_detects_roundtrip_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = snapshot()
    original["accounts"]["primary"]["observed"]["tags"]["literal"] = "$DONT_EXPAND" * 4000
    monkeypatch.setattr(provider_network, "selected_scopes", lambda: SCOPES)
    monkeypatch.setattr(provider_network, "snapshot_accounts", lambda _: original)
    monkeypatch.setattr(
        provider_network,
        "azd_value",
        lambda name, **_: "true" if name == provider_network.ENABLE_ENV else "e30=",
    )

    def inspect_file(command: list[str]) -> None:
        assert command[:4] == ["azd", "env", "set", "--file"]
        assert sum(map(len, command)) < 1000
        content = Path(command[4]).read_text(encoding="utf-8")
        assert len(content) > 32767
        assert "$" not in content
        assert provider_network.decode_snapshot(content.strip().split("=", 1)[1]) == original

    monkeypatch.setattr(provider_network, "run", inspect_file)
    with pytest.raises(CommandError, match="exact account snapshot"):
        provider_network.prepare_snapshot()


@pytest.mark.parametrize("value", ["{}", "not-base64", "W10=", "bnVsbA=="])
def test_account_snapshot_encoding_fails_closed(value: str) -> None:
    with pytest.raises(CommandError, match="snapshot"):
        provider_network.decode_snapshot(value)


@pytest.mark.parametrize("kind", ["Create", "Delete", "Replace", "Ignore", "Unsupported", "Deploy"])
def test_what_if_requires_existing_account_modify_only(kind: str) -> None:
    result = what_if()
    result["changes"][0]["changeType"] = kind
    with pytest.raises(CommandError, match="Modify/NoChange"):
        provider_network.validate_what_if(result, snapshot())


@pytest.mark.parametrize(
    "field", ["tags", "identity", "encryption", "subdomain", "project", "auth", "outbound"]
)
def test_what_if_rejects_unrelated_account_drift(field: str) -> None:
    result = what_if()
    target = result["changes"][0]["after"]
    if field == "tags":
        target["tags"] = {}
    elif field == "identity":
        target["identity"] = {"type": "None"}
    else:
        key = {
            "encryption": "encryption",
            "subdomain": "customSubDomainName",
            "project": "defaultProject",
            "auth": "disableLocalAuth",
            "outbound": "allowedFqdnList",
        }[field]
        target["properties"][key] = False if field == "auth" else "changed"
    with pytest.raises(CommandError):
        provider_network.validate_what_if(result, snapshot())


def test_exact_preview_and_idempotent_redeployment_are_accepted() -> None:
    result = what_if()
    provider_network.validate_what_if(result, snapshot())
    for change in result["changes"]:
        change["changeType"] = "NoChange"
        change["before"] = copy.deepcopy(change["after"])
    provider_network.validate_what_if(result, snapshot())


def test_failed_what_if_cannot_be_promoted_even_with_plausible_changes() -> None:
    result = what_if()
    result["status"] = "Failed"
    with pytest.raises(CommandError, match="complete successfully"):
        provider_network.validate_what_if(result, snapshot())


@pytest.mark.parametrize("extra", ["model", "project", "other-account", "ai-pe", "unexpected-dns"])
def test_what_if_rejects_unapproved_ai_resources(extra: str) -> None:
    paths = {
        "model": SCOPES["primary"] + "/deployments/preserved",
        "project": SCOPES["secondary"] + "/projects/preserved",
        "other-account": f"{GROUP}/providers/Microsoft.CognitiveServices/accounts/unapproved",
        "ai-pe": f"{GROUP}/providers/Microsoft.Network/privateEndpoints/unapproved",
        "unexpected-dns": (
            f"{GROUP}/providers/Microsoft.Network/privateDnsZones/unapproved.example.test"
        ),
    }
    result = what_if()
    result["changes"].append(
        {
            "resourceId": paths[extra],
            "changeType": "Create",
            "after": {
                "properties": {
                    "privateLinkServiceConnections": [
                        {
                            "properties": {"groupIds": ["account"]},
                        }
                    ]
                }
            },
        }
    )
    with pytest.raises(CommandError):
        provider_network.validate_what_if(result, snapshot())


def test_preservation_allows_approved_network_delta_and_preserves_identity_projects_models() -> (
    None
):
    before: dict[str, Any] = {
        "schema_version": "hosted-preservation-v1",
        "group_id": GROUP,
        "group_tags": {"owner": "keep"},
        "accounts": {
            scope: {
                "account": provider_network.observed_account(account(profile)),
                "deployments": [{"name": "existing-model", "capacity": 10}],
                "projects": [{"name": "existing-project"}],
            }
            for profile, scope in SCOPES.items()
        },
        "role_assignments": {"original": {"principalId": "operator", "scope": GROUP}},
    }
    after = copy.deepcopy(before)
    for item in after["accounts"].values():
        item["account"] = provider_network.public_network(item["account"])
    with pytest.raises(CommandError):
        preservation.compare(before, after)
    preservation.compare(before, after, allow_provider_public_network=True)
    after["accounts"][SCOPES["secondary"]]["account"]["identity"]["principalId"] = "replaced"
    with pytest.raises(CommandError):
        preservation.compare(before, after, allow_provider_public_network=True)


def test_public_provider_connectivity_rejects_stale_private_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("10.42.1.5", 443))],
    )
    with pytest.raises(ValueError, match="must resolve publicly"):
        runtime_probe.private_connectivity("https://provider.example.test", private_required=False)


def test_preprovision_requires_a_current_preview_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {provider_network.ENABLE_ENV: "true"}
    monkeypatch.setattr(
        provider_network,
        "azd_value",
        lambda name, **_kwargs: state.get(name),
    )
    monkeypatch.setattr(provider_network, "review_digest", lambda: "a" * 64)
    with pytest.raises(CommandError, match="absent or stale"):
        provider_network.require_review()
    state[provider_network.REVIEW_ENV] = "a" * 64
    provider_network.require_review()
    monkeypatch.setattr(provider_network, "review_digest", lambda: "b" * 64)
    with pytest.raises(CommandError, match="absent or stale"):
        provider_network.require_review()


@pytest.mark.parametrize(
    "suffix",
    [
        "cognitiveservices.azure.com",
        "openai.azure.com",
        "services.ai.azure.com",
    ],
)
def test_endpoint_selector_accepts_exact_returned_root_without_rewriting(suffix: str) -> None:
    original: dict[str, Any] = {
        "properties": {
            "endpoint": "https://synthetic.cognitiveservices.azure.com",
            "endpoints": {"operator-selected-key": f"https://synthetic.{suffix}/"},
        },
    }
    unchanged = copy.deepcopy(original)
    preflight._validate_endpoint_selection(original, "operator-selected-key")
    preflight._validate_endpoint_selection(original, "")
    assert original == unchanged
    with pytest.raises(CommandError, match="no fallback"):
        preflight._validate_endpoint_selection(original, "missing-explicit-key")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://region.api.cognitive.microsoft.com/",
        "https://synthetic.openai.azure.com/openai",
        "https://synthetic.services.ai.azure.com/api/projects/project",
        "https://user:synthetic@synthetic.openai.azure.com",
        "https://:synthetic@synthetic.openai.azure.com",
        "https://synthetic.openai.azure.com?api-version=synthetic",
        "https://synthetic.openai.azure.com#fragment",
        "https://synthetic.openai.azure.com:444",
        "https://synthetic.openai.azure.com:invalid",
        "https://synthetic.openai.azure.com\n",
        "http://synthetic.openai.azure.com",
    ],
)
def test_selected_endpoint_rejects_nonroots_without_falling_back(endpoint: str) -> None:
    original: dict[str, Any] = {
        "properties": {
            "endpoint": "https://synthetic.cognitiveservices.azure.com",
            "endpoints": {"operator-selected-key": endpoint},
        },
    }
    with pytest.raises(CommandError):
        preflight._validate_endpoint_selection(original, "operator-selected-key")
    assert original["properties"]["endpoints"]["operator-selected-key"] == endpoint


def test_endpoint_inspection_returns_actual_map_without_writes_or_route_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = account("primary")
    original["properties"]["endpoints"] = {
        "actual-key-not-guessed": "https://synthetic.openai.azure.com/",
    }
    monkeypatch.setattr(provider_network, "selected_scopes", lambda: {"primary": SCOPES["primary"]})
    monkeypatch.setattr(provider_network, "read_account", lambda _: original)
    monkeypatch.setattr(provider_network, "run", lambda _: pytest.fail("No local/cloud writes"))
    inspected = provider_network.inspect_endpoints()
    assert inspected["routing_proven"] is False
    assert inspected["accounts"]["primary"]["endpoint"] == original["properties"]["endpoint"]
    assert inspected["accounts"]["primary"]["endpoints"] == original["properties"]["endpoints"]
