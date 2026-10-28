from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_storage_and_key_vault_remain_private_and_identity_based() -> None:
    storage = _read("infra/modules/storage.bicep")
    assert "allowBlobPublicAccess: false" in storage
    assert "allowCrossTenantReplication: false" in storage
    assert "allowSharedKeyAccess: false" in storage
    assert "defaultToOAuthAuthentication: true" in storage
    assert "publicNetworkAccess: 'Disabled'" in storage
    assert storage.count("Microsoft.Network/privateEndpoints@") == 2
    assert "queueServices" not in storage
    assert "isVersioningEnabled: false" in storage
    assert storage.count("enabled: false") == 2
    assert storage.count("blobServices/containers@") == 2

    vault = _read("infra/modules/key-vault.bicep")
    assert "enablePurgeProtection: true" in vault
    assert "enableRbacAuthorization: true" in vault
    assert "publicNetworkAccess: 'Disabled'" in vault
    assert "Microsoft.Network/privateEndpoints@" in vault


def test_infrastructure_never_reads_service_keys() -> None:
    bicep = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "infra").rglob("*.bicep"))
    ).casefold()
    assert "listkeys(" not in bicep
    assert "allowsharedkeyaccess: true" not in bicep
    assert "foundry_api_key" not in bicep
    assert "azure_storage_connection_string" not in bicep


def test_hosted_runtime_is_single_instance_persistent_and_explicit() -> None:
    app = _read("infra/modules/app-service.bicep")
    app_settings = _read("infra/modules/platform.bicep")
    assert "type: 'SystemAssigned'" in app
    assert "AI_BENCH_RUNTIME: 'azure'" in app_settings
    assert "AI_BENCH_STATE_ROOT: '/home/data/ai-bench'" in app_settings
    assert "AI_BENCH_REQUIRE_AZURE_STORAGE: 'true'" in app_settings
    assert "AI_BENCH_ALLOW_NETWORKED_SOLUTIONS:" in app_settings
    assert "capacity: 1" in app
    assert "vnetRouteAllEnabled: true" in app
    assert "scmIpSecurityRestrictionsUseMain: false" in app
    assert "scmIpSecurityRestrictionsDefaultAction: entraOnlyScm ? 'Allow' : 'Deny'" in app
    assert "publicDemoMode && authMode == 'entra' && empty(scmAllowedCidr)" in app
    assert "name: 'authsettingsV2'" in app
    assert "enabled: false" in app
    assert "name: 'scm'" in app and "allow: false" in app
    assert "AI_BENCH_EXECUTION_BACKEND: 'azure-table-blob'" in app_settings
    assert (
        "publicDemoMode ? {\n      AI_BENCH_PUBLIC_ORIGIN: app.outputs.appUrl\n    } : {}"
        in app_settings
    )
    assert "ipSecurityRestrictionsDefaultAction:" in app
    assert "Microsoft.Insights/diagnosticSettings@" in app
    assert "workspaceId: logAnalyticsWorkspaceId" in app


def test_app_settings_preserve_existing_values_without_self_reference() -> None:
    platform = _read("infra/modules/platform.bicep")
    app_settings = _read("infra/modules/app-settings.bicep")

    assert "module appSettings './app-settings.bicep'" in platform
    assert "list(" in platform
    assert "/config/appsettings" in platform
    assert "preserveExistingAppSettings" in platform
    assert ": {}" in platform
    assert "Microsoft.Web/sites/config@" in app_settings
    assert "properties: union(existingAppSettings, managedAppSettings)" in app_settings
    assert "list(" not in app_settings


def test_foundry_reuse_and_role_assignment_use_actual_account_scope() -> None:
    main = _read("infra/main.bicep")
    foundry_info = _read("infra/modules/foundry-info.bicep")
    foundry_access = _read("infra/modules/foundry-access.bicep")

    assert "resourceGroup(foundrySubscriptionId, foundryResourceGroupName)" in main
    assert "foundryInfo!.outputs.endpoint" in main
    assert "output customSubDomainName string = account.properties.customSubDomainName" in (
        foundry_info
    )
    assert "scope: account" in foundry_access
    assert "CognitiveServices/accounts" in foundry_access


def test_role_assignment_names_resolve_before_the_new_identity_exists() -> None:
    main = _read("infra/main.bicep")
    platform = _read("infra/modules/platform.bicep")
    assert main.count("principalResourceId: appResourceId") == 2
    assert (
        "resourceId(subscription().subscriptionId, targetResourceGroupName, "
        "'Microsoft.Web/sites', appName)" in main
    )
    assert "appName: appName" in main
    assert "principalResourceId: resourceId('Microsoft.Web/sites', appName)" in platform
    for module in ("foundry-access", "storage-access"):
        text = _read(f"infra/modules/{module}.bicep")
        assert "principalResourceId, " in text
        assert "principalId: principalId" in text
        assert ", principalId, " not in text


def test_azd_hooks_use_reviewable_python_modules() -> None:
    azure_yaml = _read("azure.yaml")
    assert "python -m infra.scripts.prepare_environment" in azure_yaml
    assert "--require-provider-network-review" in azure_yaml
    assert "python -m infra.scripts.sync_auth_mode" in azure_yaml
    assert "prepackage:" in azure_yaml
    assert "predeploy:" not in azure_yaml
    assert "python -m infra.scripts.stage_package" in azure_yaml
    assert "python -m infra.scripts.verify_deployment" in azure_yaml
    assert "python -m infra.scripts.verify_deployment --bootstrap" in azure_yaml
    assert "--require-proof" not in azure_yaml


def test_release_references_only_existing_group_and_two_provider_accounts() -> None:
    main = _read("infra/main.bicep")
    assert "Microsoft.Resources/resourceGroups@" not in main
    assert "resourceGroup(maiSubscriptionId, maiResourceGroupName)" in main
    assert "maiInfo!.outputs.accountId" in main
    assert "maiAccess" in main and "foundryAccess" in main
    assert "speech-probe" not in main
    assert not (ROOT / "infra/modules/provider-network.bicep").exists()
    provider = _read("infra/modules/provider-public-network.bicep")
    assert "Microsoft.Network/privateEndpoints" not in provider
    assert "Microsoft.Network/privateDnsZones" not in provider
    assert "publicNetworkAccess: 'Enabled'" in provider
    assert "defaultAction: 'Allow'" in provider and "ipRules: []" in provider
    assert "properties: union(preservedAccount.properties" in provider
    for field in ("location", "kind", "sku", "tags", "identity"):
        assert f"{field}: preservedAccount.{field}" in provider
    assert main.count("dependsOn: [\n    foundryInfo\n    maiInfo") == 2
    assert "providerAccountSnapshots.accounts.primary.writable" in main
    assert "providerAccountSnapshots.accounts.secondary.writable" in main
    access = _read("infra/modules/storage-access.bicep")
    assert "scope: containers[index]" in access
    assert "scope: stateTable" in access
    assert "scope: storage" not in access
    assert "queueDataContributorRoleId" not in access


def test_attestation_setting_is_consistent_across_infrastructure_layers() -> None:
    setting = "AI_BENCH_BACKEND_CONFORMANCE_RECEIPT"
    for relative in (
        "infra/modules/platform.bicep",
        "infra/main.parameters.json",
        "infra/scripts/preflight.py",
        "infra/scripts/prepare_environment.py",
        "src/ai_bench/backends.py",
    ):
        assert setting in _read(relative)
    platform = _read("infra/modules/platform.bicep")
    assert "core verifies the persisted attestation before execution" in platform
    assert "backendConformanceAttestation" in _read("infra/main.bicep")


def test_speech_probe_is_isolated_identity_only_and_model_pinned() -> None:
    entrypoint = _read("infra/speech-probe.bicep")
    resources = _read("infra/modules/speech-probe-resources.bicep")

    assert "targetScope = 'subscription'" in entrypoint
    assert "param resourceGroupName string" in entrypoint
    assert "Microsoft.Resources/resourceGroups@" in entrypoint
    assert "module transcriptionResources" in entrypoint
    assert "module platform" not in entrypoint

    assert resources.count("Microsoft.CognitiveServices/accounts@2025-06-01") == 2
    assert resources.count("Microsoft.CognitiveServices/accounts/deployments@2024-10-01") == 2
    assert resources.count("disableLocalAuth: true") == 2
    assert resources.count("publicNetworkAccess: 'Enabled'") == 2
    assert resources.count("defaultAction: 'Deny'") == 2
    assert "endsWith(operatorIpCidr, '/32')" in resources
    assert resources.count("value: operatorIpRule") == 2
    assert resources.count("scope: primaryAccount") == 1
    assert resources.count("scope: maiAccount") == 1
    assert resources.count("cognitiveServicesUserRoleId") >= 3
    assert "name: 'gpt-transcribe'" in resources
    assert "version: '2026-07-28'" in resources
    assert "name: 'whisper'" in resources
    assert "version: '001'" in resources
    assert "MAI-Transcribe" not in resources
    assert "listKeys(" not in resources
    assert "apiKey" not in resources
    assert "openai-whisper-large" not in resources


def test_speech_probe_never_redeclares_or_retags_an_existing_group() -> None:
    entrypoint = _read("infra/speech-probe.bicep")
    declaration = entrypoint.split("resource probeResourceGroup ", 1)[1].split("\n}", 1)[0]
    assert "existing = {" in declaration
    assert "name: resourceGroupName" in declaration
    assert "tags:" not in declaration and "location:" not in declaration
    assert "scope: probeResourceGroup" in entrypoint
