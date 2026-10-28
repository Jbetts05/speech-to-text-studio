"""Prepare and preview the only approved updates to two existing AI accounts.

No function in this module mutates Azure. The account updates are ordinary Bicep
resources, visible in FullResourcePayloads what-if, never an opaque post-hook PATCH.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from infra.scripts._azure import CommandError, az_json, azd_value, run

API_VERSION = "2025-06-01"
SNAPSHOT_ENV = "AI_BENCH_PROVIDER_ACCOUNT_SNAPSHOTS"
ENABLE_ENV = "AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS"
SCHEMA = "existing-ai-public-network-v1"
REVIEW_ENV = "AI_BENCH_PROVIDER_NETWORK_PREVIEW_DIGEST"
# Pinned to the official 2025-06-01 AccountProperties schema. Unknown fields fail
# closed instead of being dropped from a read-back/PUT round trip.
WRITABLE_PROPERTIES = frozenset(
    {
        "allowedFqdnList",
        "allowProjectManagement",
        "amlWorkspace",
        "apiProperties",
        "associatedProjects",
        "customSubDomainName",
        "defaultProject",
        "disableLocalAuth",
        "dynamicThrottlingEnabled",
        "encryption",
        "locations",
        "migrationToken",
        "networkAcls",
        "networkInjections",
        "publicNetworkAccess",
        "raiMonitorConfig",
        "restore",
        "restrictOutboundNetworkAccess",
        "userOwnedStorage",
    }
)
READ_ONLY_PROPERTIES = frozenset(
    {
        "provisioningState",
        "endpoint",
        "internalId",
        "capabilities",
        "isMigrated",
        "skuChangeInfo",
        "privateEndpointConnections",
        "dateCreated",
        "callRateLimit",
        "quotaLimit",
        "endpoints",
        "deletionDate",
        "scheduledPurgeDate",
        "commitmentPlanAssociations",
        "abusePenalty",
    }
)
# The service exposes this metadata outside its published schema. Never drop its observed value.
PRESERVED_PROPERTIES = frozenset({"armFeatures"})


def observed_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(account[key])
        for key in ("id", "name", "type", "kind", "location", "sku", "tags", "identity")
        if key in account
    } | {
        "properties": {
            key: copy.deepcopy(value)
            for key, value in account.get("properties", {}).items()
            if key != "provisioningState"
        }
    }


def writable_account(account: dict[str, Any]) -> dict[str, Any]:
    unknown_root = set(account) - {
        "id",
        "name",
        "type",
        "kind",
        "location",
        "sku",
        "tags",
        "identity",
        "properties",
        "systemData",
        "etag",
        "apiVersion",
        "resourceGroup",
    }
    if unknown_root:
        raise CommandError(f"Unknown account fields cannot be discarded: {sorted(unknown_root)}.")
    properties = account.get("properties")
    if not isinstance(properties, dict) or not isinstance(account.get("sku"), dict):
        raise CommandError("Existing account read-back lacks its properties or SKU.")
    unknown = set(properties) - WRITABLE_PROPERTIES - READ_ONLY_PROPERTIES - PRESERVED_PROPERTIES
    if unknown:
        raise CommandError(f"Unreviewed account properties cannot be discarded: {sorted(unknown)}.")
    features = properties.get("armFeatures", [])
    if not isinstance(features, list) or any(
        not isinstance(feature, str) or not feature.startswith("Microsoft.CognitiveServices/")
        for feature in features
    ):
        raise CommandError(
            "Unrecognized account feature-registration metadata cannot be discarded."
        )
    if (
        account.get("kind") != "AIServices"
        or account["sku"].get("name") != "S0"
        or properties.get("disableLocalAuth") is not True
        or not properties.get("customSubDomainName")
        or not isinstance(properties.get("networkAcls"), dict)
        or not account.get("name")
        or not account.get("location")
    ):
        raise CommandError(
            "Existing S0 AIServices identity/subdomain/network read-back is incomplete."
        )
    if properties.get("restore") or properties.get("migrationToken"):
        raise CommandError("Account restore/migration requires a separate reviewed operation.")
    api_properties = properties.get("apiProperties") or {}
    if any(
        value and ("key" in key.casefold() or "connectionstring" in key.casefold())
        for key, value in api_properties.items()
    ):
        raise CommandError(
            "Cannot round-trip secret-bearing API properties in this Entra-only workflow."
        )
    identity = copy.deepcopy(account.get("identity"))
    if identity is not None:
        if not isinstance(identity, dict) or not identity.get("type"):
            raise CommandError("Existing account identity is not a supported ARM identity.")
        unknown_identity = set(identity) - {
            "type",
            "principalId",
            "tenantId",
            "userAssignedIdentities",
        }
        if unknown_identity:
            raise CommandError("Unknown identity fields cannot be discarded.")
        identity = {"type": identity["type"]} | (
            {"userAssignedIdentities": {key: {} for key in identity["userAssignedIdentities"]}}
            if identity.get("userAssignedIdentities") is not None
            else {}
        )
    writable_properties = {
        key: copy.deepcopy(value)
        for key, value in properties.items()
        if key in WRITABLE_PROPERTIES or key in PRESERVED_PROPERTIES
    }
    for rule_list in ("ipRules", "virtualNetworkRules"):
        writable_properties["networkAcls"].setdefault(rule_list, [])
    # ARM what-if omits empty ACL lists and null-only optional API properties.
    api_properties = {key: value for key, value in api_properties.items() if value is not None}
    if api_properties:
        writable_properties["apiProperties"] = api_properties
    else:
        writable_properties.pop("apiProperties", None)
    return {
        "name": account["name"],
        "location": account["location"],
        "kind": account["kind"],
        "sku": copy.deepcopy(account["sku"]),
        "tags": copy.deepcopy(account.get("tags") or {}),
        "identity": identity,
        "properties": writable_properties,
    }


def public_network(account: dict[str, Any]) -> dict[str, Any]:
    desired = copy.deepcopy(account)
    properties = desired["properties"]
    properties["publicNetworkAccess"] = "Enabled"
    acls = properties["networkAcls"]
    acls["defaultAction"] = "Allow"
    acls["ipRules"] = []
    return desired


def selected_scopes() -> dict[str, str]:
    subscription = str(azd_value("AZURE_SUBSCRIPTION_ID"))
    group = str(azd_value("AI_BENCH_EXISTING_RESOURCE_GROUP"))
    scopes = {}
    for profile, prefix in (("primary", "FOUNDRY"), ("secondary", "MAI")):
        provider_subscription = str(azd_value(f"AI_BENCH_{prefix}_SUBSCRIPTION_ID"))
        provider_group = str(azd_value(f"AI_BENCH_{prefix}_RESOURCE_GROUP"))
        name = str(azd_value(f"AI_BENCH_{prefix}_ACCOUNT"))
        if (
            provider_subscription.casefold() != subscription.casefold()
            or provider_group.casefold() != group.casefold()
            or not name
        ):
            raise CommandError(
                "Both provider accounts must exist in the selected subscription/group."
            )
        scopes[profile] = (
            f"/subscriptions/{subscription}/resourceGroups/{group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{name}"
        )
    if len({scope.casefold() for scope in scopes.values()}) != 2:
        raise CommandError("Two distinct existing account scopes are required.")
    return scopes


def read_account(scope: str) -> dict[str, Any]:
    account = az_json(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com{scope}?api-version={API_VERSION}",
            "--subscription",
            scope.split("/")[2],
        ]
    )
    if (
        not isinstance(account, dict)
        or str(account.get("id", "")).casefold() != scope.casefold()
        or account.get("properties", {}).get("provisioningState") != "Succeeded"
    ):
        raise CommandError("Exact existing account is missing, mismatched or not Succeeded.")
    return account


def snapshot_accounts(scopes: dict[str, str]) -> dict[str, Any]:
    accounts = {}
    for profile, scope in scopes.items():
        account = read_account(scope)
        accounts[profile] = {
            "observed": observed_account(account),
            "writable": writable_account(account),
        }
    return {"schema_version": SCHEMA, "accounts": accounts}


def inspect_endpoints() -> dict[str, Any]:
    accounts = {}
    for profile, scope in selected_scopes().items():
        account = read_account(scope)
        properties = account["properties"]
        accounts[profile] = {
            "account_id": account["id"],
            "endpoint": properties.get("endpoint"),
            "endpoints": properties.get("endpoints", {}),
            "custom_subdomain": properties.get("customSubDomainName"),
        }
    return {"accounts": accounts, "routing_proven": False}


def validate_snapshot(snapshot: dict[str, Any], scopes: dict[str, str]) -> None:
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != SCHEMA
        or set(snapshot.get("accounts", {})) != set(scopes)
    ):
        raise CommandError("Existing-account snapshot schema/profile selection is invalid.")
    for profile, scope in scopes.items():
        item = snapshot["accounts"][profile]
        observed = item.get("observed") or {}
        if str(observed.get("id", "")).casefold() != scope.casefold() or item.get(
            "writable"
        ) != writable_account(observed):
            raise CommandError(
                "Snapshot does not preserve the exact selected account's writable settings."
            )
        current = observed_account(read_account(scope))
        if current not in (observed, public_network(observed)):
            raise CommandError(
                "Existing account drifted from the reviewed snapshot. Stop; explicitly refresh "
                "the snapshot and review a new what-if, never silently overwrite it."
            )


def decode_snapshot(value: str) -> dict[str, Any]:
    try:
        snapshot = json.loads(base64.b64decode(value, validate=True))
    except (ValueError, binascii.Error, UnicodeDecodeError) as error:
        raise CommandError("Invalid base64 account snapshot JSON.") from error
    if not isinstance(snapshot, dict):
        raise CommandError("Account snapshot must be a JSON object.")
    return snapshot


def prepare_snapshot(*, refresh: bool = False) -> None:
    if azd_value(ENABLE_ENV, required=False) != "true":
        return
    scopes = selected_scopes()
    raw = str(azd_value(SNAPSHOT_ENV, required=False) or "e30=")
    snapshot = {} if refresh else decode_snapshot(raw)
    if snapshot and not refresh:
        validate_snapshot(snapshot, scopes)
        return
    snapshot = snapshot_accounts(scopes)
    encoded = base64.b64encode(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    # File transport avoids Windows' argv limit; base64 avoids AZD JSON/dotenv interpolation.
    with TemporaryDirectory(prefix="ai-bench-account-snapshot-") as temporary:
        values_file = Path(temporary) / "values.env"
        values_file.write_text(f"{SNAPSHOT_ENV}={encoded}\n", encoding="utf-8")
        run(["azd", "env", "set", "--file", str(values_file)])
    if str(azd_value(SNAPSHOT_ENV)) != encoded:
        raise CommandError("AZD did not preserve the exact account snapshot; stop before preview.")
    print("Captured local account read-back; review the network-only what-if before provisioning.")


def review_digest() -> str:
    root = Path(__file__).resolve().parents[2]
    sources = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "infra").rglob("*.bicep")
    }
    references = json.loads((root / "infra" / "main.parameters.json").read_text(encoding="utf-8"))
    values = {}
    for name, item in references["parameters"].items():
        value = item["value"]
        values[name] = (
            azd_value(value[2:-1]) if isinstance(value, str) and value.startswith("${") else value
        )
    document = {"sources": sources, "references": references, "values": values}
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def require_review() -> None:
    if azd_value(ENABLE_ENV, required=False) != "true":
        return
    if azd_value(REVIEW_ENV, required=False) != review_digest():
        raise CommandError(
            "Provider-network preview is absent or stale. Run the read-only "
            "provider_network --preview command and review the exact changes first."
        )


def validate_what_if(result: dict[str, Any], snapshot: dict[str, Any]) -> None:
    if result.get("status") != "Succeeded" or result.get("error"):
        raise CommandError(
            "ARM what-if must complete successfully before its changes can be reviewed."
        )
    expected = {
        str(item["observed"]["id"]).casefold(): item for item in snapshot["accounts"].values()
    }
    changes = result.get("changes")
    if not isinstance(changes, list):
        raise CommandError("FullResourcePayloads what-if changes are required.")
    seen: set[str] = set()
    group_scope = next(iter(expected)).split("/providers/", 1)[0]
    for change in changes:
        scope = str(change.get("resourceId", "")).casefold()
        kind = str(change.get("changeType", ""))
        if scope in expected:
            if scope in seen or kind not in {"Modify", "NoChange"}:
                raise CommandError(
                    "Existing AI accounts must be Modify/NoChange only; no Create/Replace/Ignore."
                )
            seen.add(scope)
            before, after = change.get("before"), change.get("after")
            if not isinstance(before, dict) or not isinstance(after, dict):
                raise CommandError(
                    "Both resolved account payloads are required; unresolved preview is blocked."
                )
            previous = writable_account(before)
            target = writable_account(after)
            original = expected[scope]["writable"]
            expected_features = expected[scope]["observed"]["properties"].get("armFeatures")
            for payload in (before, after):
                if (
                    "armFeatures" in payload["properties"]
                    and payload["properties"]["armFeatures"] != expected_features
                ):
                    raise CommandError("What-if changes observed account feature registrations.")
            if previous not in (original, public_network(original)) or target != public_network(
                original
            ):
                raise CommandError(
                    "What-if contains unapproved account drift, not the exact network-only update."
                )
            if kind == "NoChange" and previous != target:
                raise CommandError(
                    "What-if NoChange does not match the required account network update."
                )
            continue
        if kind in {"Delete", "Deploy", "Replace", "Unsupported"}:
            raise CommandError("Destructive or unresolved changes require separate review.")
        if (
            "/microsoft.cognitiveservices/" in scope
            and kind not in {"NoChange", "Ignore"}
            and "/providers/microsoft.authorization/roleassignments/" not in scope
        ):
            raise CommandError("No other AI account, deployment or project update is approved.")
        if scope == group_scope and kind not in {"NoChange", "Ignore"}:
            raise CommandError("The existing group must not be created, retagged or modified.")
        if "/providers/microsoft.resources/deployments/" not in scope and not (
            scope == group_scope or scope.startswith(group_scope + "/")
        ):
            raise CommandError("What-if targets a resource outside the selected existing group.")
        after = change.get("after") or {}
        if "/microsoft.network/privateendpoints/" in scope:
            connections = after.get("properties", {}).get("privateLinkServiceConnections", [])
            if any(
                "account" in connection.get("properties", {}).get("groupIds", [])
                for connection in connections
            ):
                raise CommandError(
                    "AI private endpoints are not part of the approved public-provider topology."
                )
        if "/microsoft.network/privatednszones/" in scope and kind not in {"NoChange", "Ignore"}:
            zone = scope.split("/privatednszones/", 1)[1].split("/", 1)[0]
            if zone not in {
                "privatelink.blob.core.windows.net",
                "privatelink.table.core.windows.net",
                "privatelink.vaultcore.azure.net",
            }:
                raise CommandError("Only private Blob/Table/Vault DNS zones are approved.")
    if seen != set(expected):
        raise CommandError("What-if must explicitly show both selected existing accounts.")


def preview(output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise CommandError("Choose a new local ignored path for the reviewed what-if.")
    snapshot = decode_snapshot(str(azd_value(SNAPSHOT_ENV)))
    validate_snapshot(snapshot, selected_scopes())
    root = Path(__file__).resolve().parents[2]
    template = root / "infra" / "main.bicep"
    compiled = json.loads(run(["az", "bicep", "build", "--file", str(template), "--stdout"]).stdout)
    references = json.loads((root / "infra" / "main.parameters.json").read_text(encoding="utf-8"))
    parameters = {}
    for name, reference in references["parameters"].items():
        value = reference["value"]
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            value = str(azd_value(value[2:-1]))
        parameter_type = compiled["parameters"][name]["type"].casefold()
        if parameter_type in {"bool", "int", "object", "array"} and isinstance(value, str):
            value = json.loads(value)
        parameters[name] = {"value": value}
    parameter_path = output.with_name(output.name + ".parameters.json")
    if parameter_path.exists() or parameter_path.is_symlink():
        raise CommandError("Review parameters path already exists.")
    with parameter_path.open("x", encoding="utf-8") as stream:
        json.dump({"parameters": parameters}, stream, indent=2, sort_keys=True)
    completed = run(
        [
            "az",
            "deployment",
            "sub",
            "what-if",
            "--location",
            str(azd_value("AZURE_LOCATION")),
            "--subscription",
            str(azd_value("AZURE_SUBSCRIPTION_ID")),
            "--template-file",
            str(template),
            "--parameters",
            f"@{parameter_path}",
            "--result-format",
            "FullResourcePayloads",
            "--no-pretty-print",
            "--output",
            "json",
        ]
    )
    result = json.loads(completed.stdout)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
    validate_what_if(result, snapshot)
    run(["azd", "env", "set", REVIEW_ENV, review_digest()])
    print(
        "Read-only account network preview checks passed. "
        "Review all changes before authorizing AZD provisioning."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--refresh-snapshot", action="store_true")
    action.add_argument("--inspect-endpoints", action="store_true")
    action.add_argument("--preview", type=Path)
    action.add_argument("--check-what-if", type=Path)
    arguments = parser.parse_args()
    if arguments.inspect_endpoints:
        print(json.dumps(inspect_endpoints(), indent=2, sort_keys=True))
    elif arguments.refresh_snapshot:
        prepare_snapshot(refresh=True)
    elif arguments.preview:
        preview(arguments.preview)
    else:
        snapshot = decode_snapshot(str(azd_value(SNAPSHOT_ENV)))
        validate_snapshot(snapshot, selected_scopes())
        validate_what_if(json.loads(arguments.check_what_if.read_text(encoding="utf-8")), snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
