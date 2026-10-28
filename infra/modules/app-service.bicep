targetScope = 'resourceGroup'

param name string
param appName string
param location string = resourceGroup().location
param tags object = {}
param appServiceSku string = 'B1'
param appSubnetId string
param logAnalyticsWorkspaceId string
param enablePublicAccess bool = true
param ingressAllowedCidr string = ''
param scmAllowedCidr string
param publicDemoMode bool = false
param authMode string = 'entra'

var suffix = take(uniqueString(subscription().id, resourceGroup().id, name), 6)
var serviceName = 'app'
var entraOnlyScm = publicDemoMode && authMode == 'entra' && empty(scmAllowedCidr)

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'asp-${name}-${suffix}'
  location: location
  kind: 'linux'
  tags: tags
  sku: {
    name: appServiceSku
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource app 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  kind: 'app,linux'
  tags: union(tags, {
    'azd-service-name': serviceName
  })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    clientAffinityEnabled: false
    httpsOnly: true
    publicNetworkAccess: enablePublicAccess ? 'Enabled' : 'Disabled'
    serverFarmId: plan.id
    virtualNetworkSubnetId: appSubnetId
    siteConfig: {
      alwaysOn: true
      appCommandLine: 'python -m uvicorn ai_bench.api:create_app --factory --app-dir src --host 0.0.0.0 --port 8000 --workers 1 --no-access-log'
      ftpsState: 'Disabled'
      healthCheckPath: '/health'
      http20Enabled: true
      linuxFxVersion: 'PYTHON|3.12'
      minTlsVersion: '1.2'
      ipSecurityRestrictions: empty(ingressAllowedCidr)
        ? []
        : [
            {
              action: 'Allow'
              description: 'Explicit operator network'
              ipAddress: ingressAllowedCidr
              name: 'operator-network'
              priority: 100
            }
          ]
      ipSecurityRestrictionsDefaultAction: empty(ingressAllowedCidr) ? 'Allow' : 'Deny'
      scmIpSecurityRestrictionsUseMain: false
      scmIpSecurityRestrictionsDefaultAction: entraOnlyScm ? 'Allow' : 'Deny'
      scmIpSecurityRestrictions: empty(scmAllowedCidr) ? [] : [
        {
          action: 'Allow'
          description: 'Independent operator deployment network'
          ipAddress: scmAllowedCidr
          name: 'scm-operator-network'
          priority: 100
        }
      ]
      scmMinTlsVersion: '1.2'
      vnetRouteAllEnabled: true
    }
  }
}

resource authSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: app
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: false
    }
  }
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: app
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2023-12-01' = {
  parent: app
  name: 'ftp'
  properties: {
    allow: false
  }
}

resource appDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'app-platform-diagnostics'
  scope: app
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'AppServicePlatformLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output appName string = app.name
output appUrl string = 'https://${app.properties.defaultHostName}'
output principalId string = app.identity.principalId
