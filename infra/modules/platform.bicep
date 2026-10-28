targetScope = 'resourceGroup'

param name string
param appName string
param location string = resourceGroup().location
param tags object = {}

@allowed([
  'offline'
  'entra'
  'key'
])
param authMode string

param foundryEndpoint string = ''
param appServiceSku string = 'B1'
param enableAppPublicAccess bool = true
param allowNetworkedSolutions bool = false
param ingressAllowedCidr string = ''
param enableMonitoringPublicAccess bool = true
param preserveExistingAppSettings bool = false
param publicDemoMode bool = false
param scmAllowedCidr string
param enableProviderPublicAccess bool = false
param providerAppSettings object = {}
param backendConformanceAttestation string = ''
param keyVaultName string
param storageAccountName string
param executionNamespace string

module network './network.bicep' = {
  name: 'network'
  params: {
    name: name
    location: location
    tags: tags
  }
}

module monitoring './monitoring.bicep' = {
  name: 'monitoring'
  params: {
    name: name
    location: location
    tags: tags
    enablePublicNetworkAccess: enableMonitoringPublicAccess
  }
}

module storage './storage.bicep' = {
  name: 'storage'
  params: {
    storageAccountName: storageAccountName
    location: location
    tags: tags
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    vnetId: network.outputs.vnetId
  }
}

module keyVault './key-vault.bicep' = {
  name: 'key-vault'
  params: {
    vaultName: keyVaultName
    location: location
    tags: tags
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    vnetId: network.outputs.vnetId
  }
}

module app './app-service.bicep' = {
  name: 'app-service'
  params: {
    name: name
    appName: appName
    location: location
    tags: tags
    appServiceSku: appServiceSku
    appSubnetId: network.outputs.appSubnetId
    logAnalyticsWorkspaceId: monitoring.outputs.workspaceId
    enablePublicAccess: enableAppPublicAccess
    ingressAllowedCidr: ingressAllowedCidr
    scmAllowedCidr: scmAllowedCidr
    publicDemoMode: publicDemoMode
    authMode: authMode
  }
}

module appSettings './app-settings.bicep' = {
  name: 'app-settings'
  params: {
    appName: appName
    existingAppSettings: preserveExistingAppSettings
      ? list(
          '${resourceId('Microsoft.Web/sites', appName)}/config/appsettings',
          '2023-12-01'
        ).properties
      : {}
    managedAppSettings: union(providerAppSettings, publicDemoMode ? {
      AI_BENCH_PUBLIC_ORIGIN: app.outputs.appUrl
    } : {}, {
      AI_BENCH_RUNTIME: 'azure'
      AI_BENCH_APP_PRINCIPAL_ID: app.outputs.principalId
      AI_BENCH_EXECUTION_BACKEND: 'azure-table-blob'
      AI_BENCH_STORAGE_BACKEND: 'azure-table-blob'
      AI_BENCH_AZURE_NAMESPACE: executionNamespace
      AI_BENCH_PUBLIC_DEMO_MODE: publicDemoMode ? 'true' : 'false'
      AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS: enableProviderPublicAccess ? 'true' : 'false'
      AI_BENCH_BACKEND_CONFORMANCE_RECEIPT: backendConformanceAttestation
      AI_BENCH_STATE_ROOT: '/home/data/ai-bench'
      AI_BENCH_REQUIRE_AZURE_STORAGE: 'true'
      // Operator authorization is not promotion: core verifies the persisted attestation before execution.
      AI_BENCH_ALLOW_NETWORKED_SOLUTIONS: allowNetworkedSolutions && authMode != 'offline' && length(backendConformanceAttestation) == 64
        ? 'true'
        : 'false'
      AI_BENCH_AUTH_MODE: authMode
      AI_BENCH_FOUNDRY_ENDPOINT: foundryEndpoint
      AI_BENCH_STORAGE_ACCOUNT: storage.outputs.storageAccountName
      AI_BENCH_ARTIFACT_CONTAINER: storage.outputs.artifactContainerName
      AI_BENCH_STATE_TABLE: storage.outputs.stateTableName
      AI_BENCH_AZURE_STORAGE_ACCOUNT_URL: endsWith(storage.outputs.blobEndpoint, '/')
        ? substring(storage.outputs.blobEndpoint, 0, max(0, length(storage.outputs.blobEndpoint) - 1))
        : storage.outputs.blobEndpoint
      AI_BENCH_AZURE_TABLE_ENDPOINT: storage.outputs.tableEndpoint
      AI_BENCH_AZURE_ARTIFACT_CONTAINER: storage.outputs.artifactContainerName
      AI_BENCH_AZURE_STAGING_CONTAINER: storage.outputs.stagingContainerName
      AI_BENCH_AZURE_STATE_TABLE: storage.outputs.stateTableName
      AI_BENCH_KEY_VAULT_URI: keyVault.outputs.vaultUri
      AI_BENCH_KEY_VAULT_NAME: keyVault.outputs.vaultName
      AI_BENCH_ENABLE_MONITORING_PUBLIC_ACCESS: enableMonitoringPublicAccess ? 'true' : 'false'
      APPLICATIONINSIGHTS_CONNECTION_STRING: monitoring.outputs.applicationInsightsConnectionString
      ApplicationInsightsAgent_EXTENSION_VERSION: '~3'
      ENABLE_ORYX_BUILD: 'true'
      SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
      WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'true'
      WEBSITE_VNET_ROUTE_ALL: '1'
      PYTHONUNBUFFERED: '1'
    })
  }
}

module storageAccess './storage-access.bicep' = {
  name: 'storage-access'
  params: {
    storageAccountName: storage.outputs.storageAccountName
    principalId: app.outputs.principalId
    principalResourceId: resourceId('Microsoft.Web/sites', appName)
    artifactContainerName: storage.outputs.artifactContainerName
    stagingContainerName: storage.outputs.stagingContainerName
    stateTableName: storage.outputs.stateTableName
  }
}

output appName string = app.outputs.appName
output appUrl string = app.outputs.appUrl
output appPrincipalId string = app.outputs.principalId
output storageAccountName string = storage.outputs.storageAccountName
output keyVaultName string = keyVault.outputs.vaultName
output artifactContainerName string = storage.outputs.artifactContainerName
output stagingContainerName string = storage.outputs.stagingContainerName
output stateTableName string = storage.outputs.stateTableName
output vnetId string = network.outputs.vnetId
output privateEndpointSubnetId string = network.outputs.privateEndpointSubnetId
output privateEndpointIds array = concat(
  storage.outputs.privateEndpointIds,
  [keyVault.outputs.privateEndpointId]
)
