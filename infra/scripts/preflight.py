from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import re
from collections.abc import Iterable
from typing import Any, cast

from infra.scripts._azure import (
    CommandError,
    az_json,
    azd_value,
    deployer_principal,
    require_command,
)

REQUIRED_PROVIDERS = (
    "Microsoft.Insights",
    "Microsoft.KeyVault",
    "Microsoft.Network",
    "Microsoft.OperationalInsights",
    "Microsoft.Storage",
    "Microsoft.Web",
)
REGISTER_ACTION = "Microsoft.Resources/subscriptions/providers/register/action"
ROLE_ASSIGNMENT_ACTION = "Microsoft.Authorization/roleAssignments/write"


def _matches(action: str, patterns: Iterable[str]) -> bool:
    lowered = action.casefold()
    return any(fnmatch.fnmatchcase(lowered, pattern.casefold()) for pattern in patterns)


def _permission_grants(permission: dict[str, Any], action: str) -> bool:
    actions = [str(value) for value in permission.get("actions") or []]
    not_actions = [str(value) for value in permission.get("notActions") or []]
    return _matches(action, actions) and not _matches(action, not_actions)


def _role_definition_name(role_definition_id: str) -> str:
    return role_definition_id.rstrip("/").rsplit("/", 1)[-1]


def _has_action(
    principal_id: str,
    principal_type: str,
    target_scope: str,
    action: str,
) -> bool:
    subscription_match = re.match(
        r"^/subscriptions/([^/]+)(?:/|$)", target_scope, flags=re.IGNORECASE
    )
    if subscription_match is None:
        raise CommandError(f"Cannot determine subscription from Azure scope: {target_scope}")
    subscription_id = subscription_match.group(1)
    assignment_command = [
        "role",
        "assignment",
        "list",
        "--assignee",
        principal_id,
        "--scope",
        target_scope,
        "--include-inherited",
    ]
    if principal_type == "User":
        assignment_command.append("--include-groups")
    assignment_command.extend(["--subscription", subscription_id])
    assignments = az_json(assignment_command)
    for assignment in assignments:
        role_id = str(assignment.get("roleDefinitionId", ""))
        if not role_id:
            continue
        definitions = az_json(
            [
                "role",
                "definition",
                "list",
                "--name",
                _role_definition_name(role_id),
                "--subscription",
                subscription_id,
            ]
        )
        for definition in definitions:
            if any(
                _permission_grants(permission, action)
                for permission in definition.get("permissions") or []
            ):
                return True
    return False


def _provider_state(namespace: str, subscription_id: str) -> str:
    provider = az_json(
        [
            "provider",
            "show",
            "--namespace",
            namespace,
            "--subscription",
            subscription_id,
        ]
    )
    return str(provider.get("registrationState", "Unknown"))


def _validate_execution_exposure(
    auth_mode: str,
    allow_networked: str,
    public_access: str,
    ingress_cidr: str,
    public_demo: str = "false",
    scm_cidr: str | None = None,
) -> None:
    if public_demo not in {"true", "false"}:
        raise CommandError("AI_BENCH_PUBLIC_DEMO_MODE must be true or false.")
    if auth_mode not in {"offline", "entra", "key"}:
        raise CommandError("AI_BENCH_AUTH_MODE must be offline, entra, or key.")
    if allow_networked not in {"true", "false"}:
        raise CommandError("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS must be true or false.")
    if allow_networked == "true" and auth_mode == "offline":
        raise CommandError(
            "Networked solutions cannot be enabled while AI_BENCH_AUTH_MODE is offline."
        )
    if public_access not in {"true", "false"}:
        raise CommandError("AI_BENCH_ENABLE_APP_PUBLIC_ACCESS must be true or false.")
    if public_access == "false":
        raise CommandError(
            "Private-only App Service ingress is not implemented by this baseline. "
            "Keep AI_BENCH_ENABLE_APP_PUBLIC_ACCESS=true and use the bounded operator "
            "CIDR allowlist; add App Service and SCM private endpoints plus private DNS "
            "before disabling public access."
        )
    if ingress_cidr:
        try:
            network = ipaddress.ip_network(ingress_cidr, strict=True)
        except ValueError as error:
            raise CommandError(
                "AI_BENCH_INGRESS_ALLOWED_CIDR must be a canonical IPv4 CIDR."
            ) from error
        if network.version != 4 or network.prefixlen == 0:
            raise CommandError("AI_BENCH_INGRESS_ALLOWED_CIDR must be a bounded IPv4 network.")
    if scm_cidr is not None and not (
        public_demo == "true" and auth_mode == "entra" and not scm_cidr
    ):
        try:
            scm_network = ipaddress.ip_network(scm_cidr, strict=True)
        except ValueError as error:
            raise CommandError(
                "AI_BENCH_SCM_ALLOWED_CIDR must be a canonical bounded IPv4 CIDR."
            ) from error
        if scm_network.version != 4 or scm_network.prefixlen == 0:
            raise CommandError("AI_BENCH_SCM_ALLOWED_CIDR must be a bounded IPv4 network.")
    if public_demo == "true" and (
        auth_mode != "entra" or public_access != "true" or ingress_cidr or scm_cidr is None
    ):
        raise CommandError(
            "Public demo requires Entra provider auth, public unrestricted HTTPS main "
            "ingress; SCM requires either a bounded CIDR or explicit Entra-only demo mode."
        )
    if (
        allow_networked == "true"
        and public_access == "true"
        and not ingress_cidr
        and public_demo != "true"
    ):
        raise CommandError(
            "Public ingress with networked solutions requires an explicit "
            "AI_BENCH_INGRESS_ALLOWED_CIDR. Configure an operator allowlist before "
            "enabling paid/provider execution."
        )


def _validate_provider_account(account: dict[str, Any]) -> None:
    properties = account.get("properties") or {}
    if (
        account.get("kind") != "AIServices"
        or (account.get("sku") or {}).get("name") != "S0"
        or properties.get("disableLocalAuth") is not True
    ):
        raise CommandError(
            "Existing AI account must remain S0 AIServices with local auth disabled."
        )


def _validate_endpoint_selection(account: dict[str, Any], key: str) -> None:
    from urllib.parse import urlsplit

    properties = account.get("properties") or {}
    endpoints = properties.get("endpoints") or {}
    if key and key not in endpoints:
        raise CommandError("Selected ARM endpoint-map key is absent; no fallback is permitted.")
    endpoint = endpoints.get(key) if key else properties.get("endpoint")
    if not isinstance(endpoint, str):
        raise CommandError("Selected ARM endpoint is absent or is not a string.")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise CommandError("Selected ARM endpoint is not a valid HTTPS account root.") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or endpoint != endpoint.strip()
        or any(ord(character) < 33 or ord(character) > 126 for character in endpoint)
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"\.(?:cognitiveservices\.azure\.com|openai\.azure\.com|services\.ai\.azure\.com)",
            parsed.hostname,
        )
        is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port not in {None, 443}
    ):
        raise CommandError(
            "Select an exact ARM-returned HTTPS custom account root, not a regional host, "
            "service/project path or credential-bearing URL. Endpoints are never rewritten."
        )


def _validate_deployments(account: dict[str, Any], gpt_name: str, whisper_name: str) -> None:
    expected = {
        gpt_name: ("gpt-transcribe", "2026-07-28", "GlobalStandard", 10),
        whisper_name: ("whisper", "001", "Standard", 3),
    }
    if not gpt_name or not whisper_name or gpt_name == whisper_name:
        raise CommandError("Public demo requires two distinct existing model deployment names.")
    for name, pin in expected.items():
        scope = str(account["id"])
        deployment = az_json(
            [
                "rest",
                "--method",
                "get",
                "--url",
                f"https://management.azure.com{scope}/deployments/{name}?api-version=2024-10-01",
                "--subscription",
                scope.split("/")[2],
            ]
        )
        model = (deployment.get("properties") or {}).get("model") or {}
        sku = deployment.get("sku") or {}
        if (model.get("name"), model.get("version"), sku.get("name"), sku.get("capacity")) != pin:
            raise CommandError(
                "Existing model deployment differs from approved model/version/SKU/capacity."
            )


def _validate_storage_target(subscription: str, group: str, name: str) -> None:
    if not re.fullmatch(r"[a-z0-9]{3,24}", name):
        raise CommandError("Explicit new application storage account name is required.")
    try:
        account = az_json(
            [
                "storage",
                "account",
                "show",
                "--name",
                name,
                "--resource-group",
                group,
                "--subscription",
                subscription,
            ]
        )
    except CommandError as error:
        if re.search(r"\bResourceNotFound\b", str(error), re.IGNORECASE):
            return
        raise
    if account.get("tags", {}).get("ai-bench-storage-policy") != "ten-day-delete-v1":
        raise CommandError("Refusing to change retention on storage not created for this release.")
    blob = az_json(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com{account['id']}/blobServices/default?api-version=2023-05-01",
            "--subscription",
            subscription,
        ]
    ).get("properties", {})
    if (
        blob.get("deleteRetentionPolicy", {}).get("enabled") is not False
        or blob.get("containerDeleteRetentionPolicy", {}).get("enabled") is not False
        or blob.get("isVersioningEnabled") is not False
    ):
        raise CommandError(
            "Existing storage recovery policy changed; explicit operator review is required."
        )


def _validate_hosted_provider_settings(subscription: str, group: str, app_name: str) -> None:
    if not app_name:
        return
    names = az_json(
        [
            "webapp",
            "config",
            "appsettings",
            "list",
            "--name",
            app_name,
            "--resource-group",
            group,
            "--subscription",
            subscription,
            "--query",
            "[?name=='AI_BENCH_SPEECH_PROVIDER_CONFIG'].name",
        ]
    )
    if not isinstance(names, list) or names:
        raise CommandError(
            "Hosted speech cannot use a local provider JSON fallback. Review "
            "AI_BENCH_SPEECH_PROVIDER_CONFIG explicitly; preflight never deletes settings."
        )


def _capacity_available(
    records: list[dict[str, Any]], aliases: set[str], addition: int, label: str
) -> None:
    matches = []
    for record in records:
        name = record.get("name", {})
        value = name.get("value", "")
        if value == "*":
            value = name.get("localizedValue", "")
        if re.sub(r"[^a-z]", "", str(value).casefold()) in aliases:
            matches.append(record)
    if len(matches) != 1:
        raise CommandError(
            f"Quota response did not identify {label}; obtain explicit capacity evidence."
        )
    current, limit = matches[0].get("currentValue"), matches[0].get("limit")
    if not isinstance(current, int | float) or not isinstance(limit, int | float):
        raise CommandError(f"Quota response for {label} is incomplete.")
    if current + addition > limit:
        raise CommandError(f"Insufficient {label} quota for {addition} additional resources.")


def _regional_capacity(subscription: str, location: str) -> None:
    regional_core_name = "coresusagein" + re.sub(r"[^a-z]", "", location.casefold())
    for namespace, api, checks in (
        (
            "Microsoft.Web",
            "2024-11-01",
            (
                (
                    {"virtualmachines", "vms", "totalregionalvms", regional_core_name},
                    1,
                    "App Service regional VM capacity",
                ),
            ),
        ),
        ("Microsoft.Storage", "2023-05-01", (({"storageaccounts"}, 1, "Storage accounts"),)),
        (
            "Microsoft.Network",
            "2023-11-01",
            (
                ({"virtualnetworks"}, 1, "VNets"),
                ({"privateendpoints"}, 3, "private endpoints"),
                ({"networkinterfaces"}, 3, "NICs"),
            ),
        ),
    ):
        response = az_json(
            [
                "rest",
                "--method",
                "get",
                "--url",
                f"https://management.azure.com/subscriptions/{subscription}/providers/{namespace}/locations/{location}/usages?api-version={api}",
                "--subscription",
                subscription,
            ]
        )
        for aliases, addition, label in checks:
            _capacity_available(response.get("value", []), aliases, addition, label)


def _validate_key_vault_mode_access(
    auth_mode: str,
    mode_state: str,
    operator_private_access: str,
) -> None:
    if mode_state not in {"fresh", "clean", "key", "key-pending", "unknown"}:
        raise CommandError(
            "AI_BENCH_KEY_VAULT_MODE_STATE is invalid deployment state. "
            "Create a new AZD environment or set it to the verified current state."
        )
    if operator_private_access not in {"true", "false"}:
        raise CommandError("AI_BENCH_OPERATOR_HAS_PRIVATE_KEY_VAULT_ACCESS must be true or false.")
    requires_data_plane = auth_mode == "key" or mode_state in {
        "key",
        "key-pending",
        "unknown",
    }
    if requires_data_plane and operator_private_access != "true":
        raise CommandError(
            "This authentication-mode operation requires Key Vault data-plane access "
            "through the deployment VNet and private DNS. Run from a connected trusted "
            "host and set AI_BENCH_OPERATOR_HAS_PRIVATE_KEY_VAULT_ACCESS=true only "
            "after verifying that connectivity."
        )


def _foundry(
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> dict[str, Any]:
    account = az_json(
        [
            "cognitiveservices",
            "account",
            "show",
            "--subscription",
            subscription_id,
            "--resource-group",
            resource_group,
            "--name",
            account_name,
        ]
    )
    if not isinstance(account, dict):
        raise CommandError("Azure returned an invalid Foundry account response.")
    properties = account.get("properties") or {}
    endpoint = str(properties.get("endpoint", ""))
    custom_subdomain = str(properties.get("customSubDomainName", ""))
    if not endpoint or not custom_subdomain:
        raise CommandError(
            "The selected Foundry account must expose its actual endpoint and custom "
            "subdomain before managed-identity authentication can be configured."
        )
    return cast(dict[str, Any], account)


def run_preflight() -> None:
    require_command("az")
    require_command("azd")

    subscription_id = str(azd_value("AZURE_SUBSCRIPTION_ID"))
    account = az_json(["account", "show", "--subscription", subscription_id])
    if str(account["id"]).casefold() != subscription_id.casefold():
        raise CommandError("Azure CLI did not select the AZD deployment subscription.")
    subscription_scope = f"/subscriptions/{subscription_id}"
    principal_id, principal_type = deployer_principal(subscription_id)

    auth_mode = str(azd_value("AI_BENCH_AUTH_MODE")).casefold()
    allow_networked = str(azd_value("AI_BENCH_ALLOW_NETWORKED_SOLUTIONS")).casefold()
    public_access = str(azd_value("AI_BENCH_ENABLE_APP_PUBLIC_ACCESS")).casefold()
    ingress_cidr = str(azd_value("AI_BENCH_INGRESS_ALLOWED_CIDR") or "")
    public_demo = str(azd_value("AI_BENCH_PUBLIC_DEMO_MODE", required=False) or "false")
    provider_public_access = str(
        azd_value("AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS", required=False) or "false"
    )
    if provider_public_access not in {"true", "false"}:
        raise CommandError("AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS must be true or false.")
    if public_demo == "true" and provider_public_access != "true":
        raise CommandError(
            "Public hosted release requires the explicitly reviewed public-provider network update."
        )
    if provider_public_access == "true" and auth_mode != "entra":
        raise CommandError(
            "The approved public-provider network update requires Entra-only authentication."
        )
    if provider_public_access == "true":
        from infra.scripts.provider_network import (
            SNAPSHOT_ENV,
            decode_snapshot,
            selected_scopes,
            validate_snapshot,
        )

        validate_snapshot(decode_snapshot(str(azd_value(SNAPSHOT_ENV))), selected_scopes())
    scm_cidr = azd_value("AI_BENCH_SCM_ALLOWED_CIDR", required=False)
    _validate_execution_exposure(
        auth_mode,
        allow_networked,
        public_access,
        ingress_cidr,
        public_demo,
        scm_cidr,
    )
    attestation = str(azd_value("AI_BENCH_BACKEND_CONFORMANCE_RECEIPT", required=False) or "")
    namespace = str(azd_value("AI_BENCH_AZURE_NAMESPACE", required=False) or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", namespace):
        raise CommandError(
            "AI_BENCH_AZURE_NAMESPACE must be a stable portable Table partition identity."
        )
    if allow_networked == "true" and not re.fullmatch(r"[a-f0-9]{64}", attestation):
        raise CommandError(
            "Network enablement requires the SHA-256 identity of real Azure backend "
            "conformance evidence; the runtime must verify the persisted attestation, "
            "not merely this hash format, before promotion."
        )
    mode_state = str(
        azd_value("AI_BENCH_KEY_VAULT_MODE_STATE", required=False) or "unknown"
    ).casefold()
    operator_private_access = str(
        azd_value(
            "AI_BENCH_OPERATOR_HAS_PRIVATE_KEY_VAULT_ACCESS",
            required=False,
        )
        or "false"
    ).casefold()
    _validate_key_vault_mode_access(auth_mode, mode_state, operator_private_access)
    monitoring_public_access = str(azd_value("AI_BENCH_ENABLE_MONITORING_PUBLIC_ACCESS")).casefold()
    if monitoring_public_access not in {"true", "false"}:
        raise CommandError("AI_BENCH_ENABLE_MONITORING_PUBLIC_ACCESS must be true or false.")
    if monitoring_public_access != "true":
        raise CommandError(
            "This template has no AMPLS; monitoring public access must remain enabled."
        )

    existing_resource_group = str(azd_value("AI_BENCH_EXISTING_RESOURCE_GROUP") or "")
    if not existing_resource_group:
        raise CommandError("AI_BENCH_EXISTING_RESOURCE_GROUP is required; no new group is allowed.")
    target_scope = (
        f"{subscription_scope}/resourceGroups/{existing_resource_group}"
        if existing_resource_group
        else subscription_scope
    )

    if existing_resource_group:
        az_json(
            [
                "group",
                "show",
                "--name",
                existing_resource_group,
                "--subscription",
                subscription_id,
            ]
        )
    _validate_storage_target(
        subscription_id, existing_resource_group, str(azd_value("AI_BENCH_STORAGE_ACCOUNT"))
    )
    _validate_hosted_provider_settings(
        subscription_id,
        existing_resource_group,
        str(azd_value("AI_BENCH_APP_NAME", required=False) or ""),
    )

    providers = list(REQUIRED_PROVIDERS)
    if auth_mode in {"entra", "key"}:
        providers.append("Microsoft.CognitiveServices")

    unregistered = [
        namespace
        for namespace in providers
        if _provider_state(namespace, subscription_id).casefold() != "registered"
    ]
    if unregistered:
        permission = _has_action(
            principal_id,
            principal_type,
            subscription_scope,
            REGISTER_ACTION,
        )
        suffix = (
            "The deployer can register them."
            if permission
            else f"The deployer lacks {REGISTER_ACTION} at {subscription_scope}."
        )
        raise CommandError(
            f"Required providers are not registered: {', '.join(unregistered)}. "
            f"{suffix} Register them explicitly before deployment."
        )

    if not _has_action(
        principal_id,
        principal_type,
        target_scope,
        ROLE_ASSIGNMENT_ACTION,
    ):
        raise CommandError(
            f"The deployer lacks {ROLE_ASSIGNMENT_ACTION} at {target_scope}. "
            "Contributor is insufficient; use User Access Administrator, Role Based "
            "Access Control Administrator, Owner, or a least-privilege custom role."
        )

    if auth_mode in {"entra", "key"}:
        foundry_subscription = str(azd_value("AI_BENCH_FOUNDRY_SUBSCRIPTION_ID"))
        foundry_group = str(azd_value("AI_BENCH_FOUNDRY_RESOURCE_GROUP"))
        foundry_name = str(azd_value("AI_BENCH_FOUNDRY_ACCOUNT"))
        if not foundry_group or not foundry_name:
            raise CommandError(
                "Foundry resource group and account are required for entra or key mode."
            )
        foundry = _foundry(foundry_subscription, foundry_group, foundry_name)
        foundry_scope = str(foundry["id"])
        if not _has_action(
            principal_id,
            principal_type,
            foundry_scope,
            ROLE_ASSIGNMENT_ACTION,
        ):
            raise CommandError(
                f"The deployer lacks {ROLE_ASSIGNMENT_ACTION} at the actual Foundry "
                f"account scope {foundry_scope}. Request access at that resource."
            )
        if public_demo == "true" or provider_public_access == "true":
            mai_subscription = str(azd_value("AI_BENCH_MAI_SUBSCRIPTION_ID"))
            mai_group = str(azd_value("AI_BENCH_MAI_RESOURCE_GROUP"))
            mai_name = str(azd_value("AI_BENCH_MAI_ACCOUNT"))
            if (
                foundry_subscription.casefold() != subscription_id.casefold()
                or mai_subscription.casefold() != subscription_id.casefold()
                or foundry_group.casefold() != existing_resource_group.casefold()
                or mai_group.casefold() != existing_resource_group.casefold()
                or foundry_name.casefold() == mai_name.casefold()
            ):
                raise CommandError(
                    "Hosted release must reuse both distinct accounts "
                    "in the selected existing group."
                )
            mai = _foundry(mai_subscription, mai_group, mai_name)
            _validate_provider_account(foundry)
            _validate_provider_account(mai)
            for provider, key_setting in (
                (foundry, "AI_BENCH_SPEECH_ENDPOINT_KEY"),
                (foundry, "AI_BENCH_OPENAI_ENDPOINT_KEY"),
                (mai, "AI_BENCH_MAI_ENDPOINT_KEY"),
            ):
                _validate_endpoint_selection(
                    provider, str(azd_value(key_setting, required=False) or "")
                )
            _validate_deployments(
                foundry,
                str(azd_value("AI_BENCH_GPT_TRANSCRIBE_DEPLOYMENT")),
                str(azd_value("AI_BENCH_WHISPER_DEPLOYMENT")),
            )
            for provider in (foundry, mai):
                for action in (
                    ROLE_ASSIGNMENT_ACTION,
                    "Microsoft.CognitiveServices/accounts/write",
                ):
                    if not _has_action(principal_id, principal_type, str(provider["id"]), action):
                        raise CommandError(f"The deployer lacks {action} at {provider['id']}.")

    location = str(azd_value("AZURE_LOCATION"))
    sku = str(azd_value("AI_BENCH_APP_SERVICE_SKU"))
    if public_demo == "true" and (
        location.replace(" ", "").casefold() != "swedencentral" or sku != "B1"
    ):
        raise CommandError(
            "The approved hosted release uses Sweden Central Linux B1, capacity one."
        )
    locations = az_json(
        [
            "appservice",
            "list-locations",
            "--sku",
            sku,
            "--linux-workers-enabled",
            "--subscription",
            subscription_id,
        ]
    )
    available = {str(item.get("name", "")).replace(" ", "").casefold() for item in locations}
    if location.replace(" ", "").casefold() not in available:
        raise CommandError(
            f"Linux App Service SKU {sku} is not reported as available in {location}."
        )
    if public_demo == "true":
        _regional_capacity(subscription_id, location)

    print(
        "Azure preflight passed for provider registration, role-assignment "
        "permissions, existing resources, Foundry custom subdomain, and App Service "
        "location availability. Public-demo regional quota checks passed when selected. "
        "DNS-zone, Vault, monitoring, policy and SKU capacity still require the documented "
        "review; unsupported quota APIs are not proof of unlimited capacity."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Azure deployment preflight.")
    parser.parse_args()
    run_preflight()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
