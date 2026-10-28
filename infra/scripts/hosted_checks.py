"""Read-only ARM verification of the hosted topology, not a deployment proof."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from infra.scripts._azure import CommandError

BLOB_ROLE = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
TABLE_ROLE = "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"
AI_ROLE = "a97b65f3-24c7-4388-baec-2e87135dc908"
VAULT_ROLE = "4633458b-17de-408a-b874-0445c86b69e6"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CommandError(message)


def verify_role(assignments: list[dict[str, Any]], principal: str, scope: str, role: str) -> None:
    require(
        any(
            str(item.get("principalId", "")).casefold() == principal.casefold()
            and str(item.get("scope", "")).casefold() == scope.casefold()
            and str(item.get("roleDefinitionId", "")).rsplit("/", 1)[-1].casefold() == role
            for item in assignments
        ),
        f"Missing exact role definition {role} for the app identity at {scope}.",
    )


def verify_arm(
    read: Callable[[list[str]], Any],
    subscription: str,
    group: str,
    app_name: str,
    storage_name: str,
    vault_name: str,
    auth_mode: str,
    provider_scopes: tuple[str, ...],
    *,
    public_demo: bool,
    scm_cidr: str,
    expected_endpoint_ids: tuple[str, ...] = (),
) -> str:
    def get(resource_id: str, api: str) -> dict[str, Any]:
        value = read(
            [
                "rest",
                "--method",
                "get",
                "--url",
                f"https://management.azure.com{resource_id}?api-version={api}",
                "--subscription",
                resource_id.split("/")[2],
            ]
        )
        require(isinstance(value, dict), "Invalid ARM object response.")
        return dict(value)

    common = ["--resource-group", group, "--subscription", subscription]
    if public_demo:
        require(
            len(provider_scopes) == 2
            and len(set(scope.casefold() for scope in provider_scopes)) == 2,
            "Public demo must identify both distinct existing provider accounts.",
        )
    app = read(["webapp", "show", "--name", app_name, *common])
    principal = str((app.get("identity") or {}).get("principalId", ""))
    subnet = str(app.get("virtualNetworkSubnetId", ""))
    vnet = subnet.rsplit("/subnets/", 1)[0]
    require(
        bool(principal) and "/subnets/" in subnet and app.get("httpsOnly") is True,
        "App must have managed identity, HTTPS-only and VNet integration.",
    )
    require(app.get("publicNetworkAccess") == "Enabled", "Main HTTPS ingress must be enabled.")
    app_id = str(app["id"])
    config = read(["webapp", "config", "show", "--name", app_name, *common])
    entra_only_scm = public_demo and auth_mode == "entra" and not scm_cidr
    require(
        config.get("scmIpSecurityRestrictionsUseMain") is False
        and config.get("scmIpSecurityRestrictionsDefaultAction")
        == ("Allow" if entra_only_scm else "Deny")
        and config.get("scmMinTlsVersion") in {"1.2", "1.3"}
        and config.get("minTlsVersion") in {"1.2", "1.3"}
        and config.get("ftpsState") == "Disabled"
        and config.get("vnetRouteAllEnabled") is True,
        "SCM must match its independent reviewed policy with TLS, FTPS disabled and VNet routing.",
    )
    scm_allows = [
        item
        for item in config.get("scmIpSecurityRestrictions", [])
        if item.get("action") == "Allow"
    ]
    if entra_only_scm:
        require(
            all(
                rule.get("action") == "Allow"
                and rule.get("ipAddress") == "Any"
                and not rule.get("headers")
                and not rule.get("vnetSubnetResourceId")
                for rule in config.get("scmIpSecurityRestrictions", [])
            ),
            "Entra-only SCM must have no IP allowlist or additional network rules.",
        )
    else:
        require(
            len(scm_allows) == 1 and scm_allows[0].get("ipAddress") == scm_cidr and bool(scm_cidr),
            "SCM allow rules do not match the exact reviewed operator CIDR.",
        )
    plan = get(str(app["serverFarmId"]), "2023-12-01")
    require(
        plan.get("properties", {}).get("reserved") is True
        and plan.get("sku", {}).get("capacity") == 1,
        "The app requires one Linux App Service instance.",
    )
    if public_demo:
        require(
            plan.get("sku", {}).get("name") == "B1", "Hosted demo requires the reviewed B1 SKU."
        )
        require(
            config.get("ipSecurityRestrictionsDefaultAction") == "Allow"
            and not [
                item
                for item in config.get("ipSecurityRestrictions", [])
                if item.get("ipAddress") != "Any"
            ],
            "Public demo main ingress is unexpectedly restricted.",
        )
    auth = get(f"{app_id}/config/authsettingsV2", "2023-12-01")
    require(
        auth.get("properties", {}).get("platform", {}).get("enabled") is False,
        "Main site must not enable Easy Auth.",
    )
    for policy in ("scm", "ftp"):
        result = get(f"{app_id}/basicPublishingCredentialsPolicies/{policy}", "2023-12-01")
        require(
            result.get("properties", {}).get("allow") is False,
            "Basic publishing credentials must be disabled.",
        )

    storage = read(["storage", "account", "show", "--name", storage_name, *common])
    storage_id = str(storage["id"])
    require(
        storage.get("publicNetworkAccess") == "Disabled"
        and storage.get("allowSharedKeyAccess") is False
        and storage.get("allowBlobPublicAccess") is False
        and storage.get("enableHttpsTrafficOnly", storage.get("supportsHttpsTrafficOnly")) is True
        and storage.get("minimumTlsVersion") == "TLS1_2",
        "Storage must disable public/shared-key access and require HTTPS/TLS.",
    )
    blob = get(f"{storage_id}/blobServices/default", "2023-05-01")["properties"]
    require(
        blob.get("deleteRetentionPolicy", {}).get("enabled") is False
        and blob.get("containerDeleteRetentionPolicy", {}).get("enabled") is False
        and blob.get("isVersioningEnabled") is False,
        "New app storage must not retain deleted upload versions or soft-deleted content.",
    )
    # An age-based account policy could erase content shared with a permanent pin.
    try:
        management_policy = get(f"{storage_id}/managementPolicies/default", "2023-05-01")
    except CommandError as error:
        if not re.search(r"\b(?:ResourceNotFound|ManagementPolicyNotFound)\b", str(error)):
            raise
        management_policy = {}
    require(
        not any(
            rule.get("enabled", True)
            for rule in management_policy.get("properties", {}).get("policy", {}).get("rules", [])
        ),
        "App storage must not have an unreviewed lifecycle deletion policy.",
    )

    vault = read(["keyvault", "show", "--name", vault_name, *common])
    vault_id = str(vault["id"])
    vp = vault.get("properties", {})
    require(
        vp.get("publicNetworkAccess") == "Disabled"
        and vp.get("enableRbacAuthorization") is True
        and vp.get("enablePurgeProtection") is True
        and vp.get("enableSoftDelete") is True,
        "Key Vault must remain private, RBAC-enabled, soft-deleted and purge-protected.",
    )
    settings = {
        str(item["name"]): str(item.get("value", ""))
        for item in read(["webapp", "config", "appsettings", "list", "--name", app_name, *common])
    }
    require(
        "AI_BENCH_SPEECH_PROVIDER_CONFIG" not in settings,
        "Hosted speech must not retain a local provider JSON fallback setting.",
    )
    if public_demo:
        require(
            bool(app.get("defaultHostName"))
            and settings.get("AI_BENCH_PUBLIC_ORIGIN") == f"https://{app['defaultHostName']}",
            "Public origin must match the actual HTTPS app hostname output, without a path.",
        )
        require(
            settings.get("AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS") == "true",
            "Public provider networking must be an explicit reviewed deployment setting.",
        )
        for scope, endpoint_names in zip(
            provider_scopes,
            (
                (
                    ("AI_BENCH_SPEECH_ENDPOINT", "AI_BENCH_SPEECH_ENDPOINT_KEY"),
                    ("AI_BENCH_OPENAI_ENDPOINT", "AI_BENCH_OPENAI_ENDPOINT_KEY"),
                ),
                (("AI_BENCH_MAI_ENDPOINT", "AI_BENCH_MAI_ENDPOINT_KEY"),),
            ),
            strict=True,
        ):
            account = get(scope, "2025-06-01")
            properties = account.get("properties", {})
            require(
                account.get("kind") == "AIServices"
                and account.get("sku", {}).get("name") == "S0"
                and properties.get("disableLocalAuth") is True
                and properties.get("publicNetworkAccess") == "Enabled"
                and properties.get("networkAcls", {}).get("defaultAction") == "Allow"
                and properties.get("networkAcls", {}).get("ipRules") == [],
                "Existing provider identity/network configuration "
                "differs from the approved baseline.",
            )
            for endpoint_name, key_name in endpoint_names:
                endpoint_key = settings.get(key_name, "")
                endpoint = (
                    properties.get("endpoints", {}).get(endpoint_key)
                    if endpoint_key
                    else properties.get("endpoint")
                )
                require(
                    bool(endpoint) and settings.get(endpoint_name) == endpoint,
                    "Provider app setting does not match its actual selected ARM endpoint.",
                )
    require(
        settings.get("AI_BENCH_RUNTIME") == "azure"
        and settings.get("AI_BENCH_EXECUTION_BACKEND") == "azure-table-blob"
        and settings.get("AI_BENCH_STORAGE_BACKEND") == "azure-table-blob"
        and bool(settings.get("AI_BENCH_AZURE_NAMESPACE"))
        and settings.get("AI_BENCH_AUTH_MODE") == auth_mode
        and settings.get("AI_BENCH_PUBLIC_DEMO_MODE") == str(public_demo).lower(),
        "Deployed runtime/backend/auth/public policy does not match the release.",
    )
    require(
        auth_mode == "key" or "AI_BENCH_FOUNDRY_API_KEY" not in settings,
        "A stale provider-key app setting remains outside key mode.",
    )
    if auth_mode == "key":
        require(
            settings.get("AI_BENCH_FOUNDRY_API_KEY", "").startswith("@Microsoft.KeyVault("),
            "Key mode must use a vault reference, never a direct key setting.",
        )
    require(
        not any(
            ("CONNECTION_STRING" in key and key != "APPLICATIONINSIGHTS_CONNECTION_STRING")
            or (key.endswith("_API_KEY") and auth_mode != "key")
            for key in settings
        ),
        "Unexpected credential-bearing app setting.",
    )
    expected_roles: set[tuple[str, str]] = set()
    for setting in ("AI_BENCH_AZURE_ARTIFACT_CONTAINER", "AI_BENCH_AZURE_STAGING_CONTAINER"):
        name = settings.get(setting, "")
        require(bool(name), f"Missing {setting}.")
        scope = f"{storage_id}/blobServices/default/containers/{name}"
        container = get(scope, "2023-05-01")
        require(
            container.get("properties", {}).get("publicAccess") == "None",
            "Container must be private.",
        )
        expected_roles.add((scope.casefold(), BLOB_ROLE))
    require(
        settings.get("AI_BENCH_AZURE_ARTIFACT_CONTAINER")
        != settings.get("AI_BENCH_AZURE_STAGING_CONTAINER"),
        "Evidence and staging containers must be distinct.",
    )
    table_name = settings.get("AI_BENCH_AZURE_STATE_TABLE", "")
    require(bool(table_name), "Missing precreated coordination table.")
    table_scope = f"{storage_id}/tableServices/default/tables/{table_name}"
    get(table_scope, "2023-05-01")
    expected_roles.add((table_scope.casefold(), TABLE_ROLE))
    if auth_mode == "entra":
        expected_roles.update((scope.casefold(), AI_ROLE) for scope in provider_scopes)
    if auth_mode == "key":
        expected_roles.add((vault_id.casefold(), VAULT_ROLE))
    assignments = [
        assignment
        for role_subscription in sorted(
            {subscription, *(scope.split("/")[2] for scope in provider_scopes)}
        )
        for assignment in read(
            [
                "role",
                "assignment",
                "list",
                "--assignee-object-id",
                principal,
                "--all",
                "--subscription",
                role_subscription,
            ]
        )
    ]
    for scope, role in expected_roles:
        verify_role(assignments, principal, scope, role)
    actual_roles = {
        (
            str(item.get("scope", "")).casefold(),
            str(item.get("roleDefinitionId", "")).rsplit("/", 1)[-1].casefold(),
        )
        for item in assignments
        if str(item.get("principalId", "")).casefold() == principal.casefold()
    }
    require(
        actual_roles == expected_roles,
        "App has unexpected/broad role assignments; review exact scopes without deleting them.",
    )

    targets = {
        (storage_id.casefold(), "blob"): {"privatelink.blob.core.windows.net"},
        (storage_id.casefold(), "table"): {"privatelink.table.core.windows.net"},
        (vault_id.casefold(), "vault"): {"privatelink.vaultcore.azure.net"},
    }
    seen: set[tuple[str, str]] = set()
    owned_ids = {value.casefold() for value in expected_endpoint_ids}
    require(
        len(owned_ids) == len(targets),
        "Exact deployment-owned private endpoint IDs are required; targets alone are insufficient.",
    )
    found_ids: set[str] = set()
    endpoints = read(["network", "private-endpoint", "list", *common])
    for endpoint in endpoints:
        endpoint_id = str(endpoint.get("id", "")).casefold()
        if endpoint_id not in owned_ids:
            continue
        found_ids.add(endpoint_id)
        connections = endpoint.get("privateLinkServiceConnections", [])
        for connection in connections:
            target = str(connection.get("privateLinkServiceId", "")).casefold()
            for group_id in connection.get("groupIds", []):
                key = (target, str(group_id))
                if key not in targets:
                    continue
                require(key not in seen, "Duplicate private endpoint target in release topology.")
                require(
                    endpoint.get("provisioningState") == "Succeeded"
                    and connection.get("privateLinkServiceConnectionState", {}).get("status")
                    == "Approved"
                    and endpoint.get("subnet", {}).get("id", "").casefold()
                    == f"{vnet}/subnets/private-endpoints".casefold()
                    and len(endpoint.get("networkInterfaces", [])) == 1,
                    "Private endpoint must be approved, provisioned, in the app subnet "
                    "and have one NIC.",
                )
                zone_groups = get(f"{endpoint['id']}/privateDnsZoneGroups", "2023-11-01").get(
                    "value", []
                )
                zone_ids = {
                    str(zone["properties"]["privateDnsZoneId"])
                    for zone_group in zone_groups
                    for zone in zone_group.get("properties", {}).get("privateDnsZoneConfigs", [])
                }
                require(
                    {value.rsplit("/", 1)[-1] for value in zone_ids} == targets[key],
                    "Private endpoint DNS zone group is incomplete or unexpected.",
                )
                for zone_id in zone_ids:
                    links = get(f"{zone_id}/virtualNetworkLinks", "2020-06-01").get("value", [])
                    require(
                        any(
                            item.get("properties", {})
                            .get("virtualNetwork", {})
                            .get("id", "")
                            .casefold()
                            == vnet.casefold()
                            and item.get("properties", {}).get("provisioningState") == "Succeeded"
                            and item.get("properties", {}).get("registrationEnabled") is False
                            for item in links
                        ),
                        "Private DNS zone lacks its successful app VNet link.",
                    )
                seen.add(key)
    require(
        seen == set(targets) and found_ids == owned_ids,
        "Required approved Blob/Table/Vault private endpoints are missing.",
    )
    return principal
