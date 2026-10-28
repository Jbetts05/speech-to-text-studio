from __future__ import annotations

import argparse
import hashlib
import re

from infra.scripts._azure import (
    CommandError,
    az_json,
    azd_value,
    require_command,
    run,
    set_azd_default,
)
from infra.scripts.preflight import run_preflight
from infra.scripts.provider_network import prepare_snapshot, require_review


def _initial_mode_state(
    subscription_id: str,
    environment_name: str,
    existing_resource_group: str,
    deployed_resource_group: str,
    target_vault_name: str = "",
) -> str:
    del environment_name
    resource_group = existing_resource_group or deployed_resource_group
    if not resource_group or not target_vault_name:
        return "unknown"
    try:
        az_json(
            [
                "keyvault",
                "show",
                "--subscription",
                subscription_id,
                "--resource-group",
                resource_group,
                "--name",
                target_vault_name,
            ]
        )
    except CommandError as error:
        # Only an exact ARM absence, not permission/network errors or RG tags, proves absence.
        if not re.search(r"\bResourceNotFound\b", str(error), re.IGNORECASE):
            return "unknown"
        try:
            deleted = az_json(["keyvault", "list-deleted", "--subscription", subscription_id])
        except CommandError:
            return "unknown"
        if not isinstance(deleted, list) or any(
            str(item.get("name", "")).casefold() == target_vault_name.casefold() for item in deleted
        ):
            return "unknown"
        return "fresh"
    return "unknown"


def _target_vault_name(subscription_id: str, resource_group: str, environment_name: str) -> str:
    configured = azd_value("AI_BENCH_KEY_VAULT_NAME", required=False)
    if configured:
        return configured
    if azd_value("AI_BENCH_APP_NAME", required=False):
        raise CommandError("Existing deployment must supply its exact AI_BENCH_KEY_VAULT_NAME.")
    identity = "|".join((subscription_id, resource_group, environment_name)).casefold()
    return f"kv-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"


def _deployed_optional_values(
    subscription_id: str, resource_group: str, environment_name: str
) -> dict[str, str]:
    if not resource_group:
        return {}
    resources = az_json(
        [
            "resource",
            "list",
            "--resource-group",
            resource_group,
            "--resource-type",
            "Microsoft.Web/sites",
            "--subscription",
            subscription_id,
        ]
    )
    apps = [
        item
        for item in resources
        if (item.get("tags") or {}).get("azd-env-name") == environment_name
        and (item.get("tags") or {}).get("azd-service-name") == "app"
    ]
    if len(apps) > 1:
        raise CommandError("Multiple App Services match the selected environment.")
    if not apps:
        return {}
    name = str(apps[0]["name"])
    settings = az_json(
        [
            "webapp",
            "config",
            "appsettings",
            "list",
            "--name",
            name,
            "--resource-group",
            resource_group,
            "--subscription",
            subscription_id,
        ]
    )
    if any(item.get("name") == "AI_BENCH_SPEECH_PROVIDER_CONFIG" for item in settings):
        raise CommandError(
            "Hosted speech cannot retain AI_BENCH_SPEECH_PROVIDER_CONFIG. Review and "
            "explicitly remove that local-file setting; preparation will not erase it."
        )
    preserved = {
        "AI_BENCH_AUTH_MODE",
        "AI_BENCH_ALLOW_NETWORKED_SOLUTIONS",
        "AI_BENCH_PUBLIC_DEMO_MODE",
        "AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS",
        "AI_BENCH_BACKEND_CONFORMANCE_RECEIPT",
        "AI_BENCH_GPT_TRANSCRIBE_DEPLOYMENT",
        "AI_BENCH_WHISPER_DEPLOYMENT",
        "AI_BENCH_FOUNDRY_SUBSCRIPTION_ID",
        "AI_BENCH_FOUNDRY_RESOURCE_GROUP",
        "AI_BENCH_FOUNDRY_ACCOUNT",
        "AI_BENCH_MAI_SUBSCRIPTION_ID",
        "AI_BENCH_MAI_RESOURCE_GROUP",
        "AI_BENCH_MAI_ACCOUNT",
        "AI_BENCH_SPEECH_ENDPOINT_KEY",
        "AI_BENCH_OPENAI_ENDPOINT_KEY",
        "AI_BENCH_MAI_ENDPOINT_KEY",
        "AI_BENCH_KEY_VAULT_NAME",
        "AI_BENCH_STORAGE_ACCOUNT",
        "AI_BENCH_AZURE_NAMESPACE",
        "AI_BENCH_ENABLE_MONITORING_PUBLIC_ACCESS",
    }
    result = {
        str(item["name"]): str(item["value"]) for item in settings if item["name"] in preserved
    }
    if not result.get("AI_BENCH_KEY_VAULT_NAME") and not azd_value(
        "AI_BENCH_KEY_VAULT_NAME", required=False
    ):
        raise CommandError(
            "Existing app has no exact vault-name setting; supply "
            "AI_BENCH_KEY_VAULT_NAME rather than generating a replacement vault."
        )
    app = az_json(
        [
            "webapp",
            "show",
            "--name",
            name,
            "--resource-group",
            resource_group,
            "--subscription",
            subscription_id,
        ]
    )
    plan = az_json(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"https://management.azure.com{app['serverFarmId']}?api-version=2023-12-01",
            "--subscription",
            subscription_id,
        ]
    )
    result["AI_BENCH_APP_SERVICE_SKU"] = str(plan["sku"]["name"])
    result["AI_BENCH_ENABLE_APP_PUBLIC_ACCESS"] = str(
        app.get("publicNetworkAccess") != "Disabled"
    ).lower()
    config = az_json(
        [
            "webapp",
            "config",
            "show",
            "--name",
            name,
            "--resource-group",
            resource_group,
            "--subscription",
            subscription_id,
        ]
    )
    for field, setting in (
        ("ipSecurityRestrictions", "AI_BENCH_INGRESS_ALLOWED_CIDR"),
        ("scmIpSecurityRestrictions", "AI_BENCH_SCM_ALLOWED_CIDR"),
    ):
        if azd_value(setting, required=False) is not None:
            continue
        rules_in_arm = config.get(field, [])
        if (
            field == "scmIpSecurityRestrictions"
            and config.get(f"{field}DefaultAction") == "Allow"
            and config.get("scmIpSecurityRestrictionsUseMain") is False
            and result.get("AI_BENCH_PUBLIC_DEMO_MODE") == "true"
            and result.get("AI_BENCH_AUTH_MODE") == "entra"
            and all(
                rule.get("ipAddress") == "Any" and rule.get("action") == "Allow"
                for rule in rules_in_arm
            )
        ):
            result[setting] = ""
            continue
        if any(
            rule.get("ipAddress") != "Any"
            and (
                rule.get("action") != "Allow"
                or rule.get("headers")
                or rule.get("vnetSubnetResourceId")
                or rule.get("tag") not in {None, "Default"}
            )
            for rule in rules_in_arm
        ):
            raise CommandError(f"Review existing {field}; it cannot be preserved as a simple CIDR.")
        rules = [
            r
            for r in config.get(field, [])
            if r.get("action") == "Allow"
            and not (
                r.get("ipAddress") == "Any"
                and field == "ipSecurityRestrictions"
                and config.get(f"{field}DefaultAction") == "Allow"
            )
        ]
        if len(rules) == 1 and rules[0].get("ipAddress") not in {None, "Any"}:
            result[setting] = str(rules[0]["ipAddress"])
        elif rules:
            raise CommandError(
                f"Review existing {field}; cannot safely map multiple rules to one CIDR."
            )
        elif config.get(f"{field}DefaultAction") == "Deny":
            raise CommandError(
                f"Refusing to replace existing default-deny {field} with public ingress."
            )
    return result


def _preserve_existing_app_settings(
    subscription_id: str,
    environment_name: str,
    existing_resource_group: str,
    deployed_resource_group: str,
) -> str:
    resource_group = existing_resource_group or deployed_resource_group
    if not resource_group:
        groups = az_json(
            [
                "group",
                "list",
                "--subscription",
                subscription_id,
                "--tag",
                f"azd-env-name={environment_name}",
            ]
        )
        if not isinstance(groups, list):
            raise CommandError("Azure returned an invalid resource-group response.")
        if len(groups) > 1:
            raise CommandError(
                "Multiple resource groups use this AZD environment tag; select an "
                "explicit existing resource group before deployment."
            )
        if not groups:
            return "false"
        resource_group = str(groups[0].get("name", ""))
        if not resource_group:
            raise CommandError("The tagged resource group does not have a name.")

    resources = az_json(
        [
            "resource",
            "list",
            "--resource-group",
            resource_group,
            "--resource-type",
            "Microsoft.Web/sites",
            "--subscription",
            subscription_id,
        ]
    )
    if not isinstance(resources, list):
        raise CommandError("Azure returned an invalid App Service response.")
    apps = [
        resource
        for resource in resources
        if str((resource.get("tags") or {}).get("azd-service-name", "")) == "app"
        and str((resource.get("tags") or {}).get("azd-env-name", "")) == environment_name
    ]
    if len(apps) > 1:
        raise CommandError(
            "Multiple App Services use this AZD environment and service tag pair "
            "in the target resource group."
        )
    return "true" if apps else "false"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare local AZD state and read-only preflight.")
    parser.add_argument("--require-provider-network-review", action="store_true")
    arguments = parser.parse_args()
    require_command("az")
    require_command("azd")

    subscription_id = str(azd_value("AZURE_SUBSCRIPTION_ID"))
    environment_name = str(azd_value("AZURE_ENV_NAME"))
    account = az_json(["account", "show", "--subscription", subscription_id])
    if str(account["id"]).casefold() != subscription_id.casefold():
        raise RuntimeError("Azure CLI did not select the AZD deployment subscription.")

    defaults = {
        "AI_BENCH_EXISTING_RESOURCE_GROUP": "",
        "AI_BENCH_AUTH_MODE": "entra",
        "AI_BENCH_FOUNDRY_SUBSCRIPTION_ID": subscription_id,
        "AI_BENCH_FOUNDRY_RESOURCE_GROUP": "",
        "AI_BENCH_FOUNDRY_ACCOUNT": "",
        "AI_BENCH_MAI_SUBSCRIPTION_ID": subscription_id,
        "AI_BENCH_MAI_RESOURCE_GROUP": "",
        "AI_BENCH_MAI_ACCOUNT": "",
        "AI_BENCH_GPT_TRANSCRIBE_DEPLOYMENT": "",
        "AI_BENCH_WHISPER_DEPLOYMENT": "",
        "AI_BENCH_SPEECH_ENDPOINT_KEY": "",
        "AI_BENCH_OPENAI_ENDPOINT_KEY": "",
        "AI_BENCH_MAI_ENDPOINT_KEY": "",
        "AI_BENCH_PUBLIC_DEMO_MODE": "false",
        "AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS": "false",
        "AI_BENCH_PROVIDER_ACCOUNT_SNAPSHOTS": "e30=",
        "AI_BENCH_SCM_ALLOWED_CIDR": "",
        "AI_BENCH_BACKEND_CONFORMANCE_RECEIPT": "",
        "AI_BENCH_AZURE_NAMESPACE": "application-v1",
        "AI_BENCH_APP_SERVICE_SKU": "B1",
        "AI_BENCH_ENABLE_APP_PUBLIC_ACCESS": "true",
        "AI_BENCH_ALLOW_NETWORKED_SOLUTIONS": "false",
        "AI_BENCH_INGRESS_ALLOWED_CIDR": "",
        "AI_BENCH_ENABLE_MONITORING_PUBLIC_ACCESS": "true",
        "AI_BENCH_OPERATOR_HAS_PRIVATE_KEY_VAULT_ACCESS": "false",
    }
    existing_group = str(azd_value("AI_BENCH_EXISTING_RESOURCE_GROUP", required=False) or "")
    if not existing_group:
        raise CommandError(
            "Select AI_BENCH_EXISTING_RESOURCE_GROUP; creating a group is not supported."
        )
    defaults.update(_deployed_optional_values(subscription_id, existing_group, environment_name))
    if "AI_BENCH_KEY_VAULT_NAME" not in defaults:
        defaults["AI_BENCH_KEY_VAULT_NAME"] = _target_vault_name(
            subscription_id, existing_group, environment_name
        )
    identity = "|".join((subscription_id, existing_group, environment_name)).casefold()
    defaults.setdefault(
        "AI_BENCH_STORAGE_ACCOUNT",
        "st" + hashlib.sha256(f"hosted-storage-v1|{identity}".encode()).hexdigest()[:22],
    )
    changed = [name for name, value in defaults.items() if set_azd_default(name, value)]
    vault_target = (
        f"/subscriptions/{subscription_id}/resourceGroups/{existing_group}"
        f"/providers/Microsoft.KeyVault/vaults/{azd_value('AI_BENCH_KEY_VAULT_NAME')}"
    )
    previous_target = azd_value("AI_BENCH_KEY_VAULT_MODE_TARGET", required=False)
    if (
        azd_value("AI_BENCH_KEY_VAULT_MODE_STATE", required=False) is None
        or (previous_target or "").casefold() != vault_target.casefold()
    ):
        existing_group = str(azd_value("AI_BENCH_EXISTING_RESOURCE_GROUP") or "")
        deployed_group = str(azd_value("AZURE_RESOURCE_GROUP", required=False) or "")
        initial_state = _initial_mode_state(
            subscription_id,
            environment_name,
            existing_group,
            deployed_group,
            str(azd_value("AI_BENCH_KEY_VAULT_NAME")),
        )
        run(["azd", "env", "set", "AI_BENCH_KEY_VAULT_MODE_STATE", initial_state])
        run(["azd", "env", "set", "AI_BENCH_KEY_VAULT_MODE_TARGET", vault_target])
        changed.append("AI_BENCH_KEY_VAULT_MODE_STATE")
    preserve_app_settings = _preserve_existing_app_settings(
        subscription_id,
        environment_name,
        str(azd_value("AI_BENCH_EXISTING_RESOURCE_GROUP") or ""),
        str(azd_value("AZURE_RESOURCE_GROUP", required=False) or ""),
    )
    if (
        azd_value("AI_BENCH_PRESERVE_EXISTING_APP_SETTINGS", required=False)
        != preserve_app_settings
    ):
        run(
            [
                "azd",
                "env",
                "set",
                "AI_BENCH_PRESERVE_EXISTING_APP_SETTINGS",
                preserve_app_settings,
            ]
        )
        changed.append("AI_BENCH_PRESERVE_EXISTING_APP_SETTINGS")
    if changed:
        print(f"Set safe AZD defaults: {', '.join(changed)}")
    else:
        print("Preserved all existing optional AZD configuration.")

    prepare_snapshot()
    run_preflight()
    if arguments.require_provider_network_review:
        require_review()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
