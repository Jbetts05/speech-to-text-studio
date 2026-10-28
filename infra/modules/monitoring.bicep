targetScope = 'resourceGroup'

param name string
param location string = resourceGroup().location
param tags object = {}
param enablePublicNetworkAccess bool = true

var suffix = take(uniqueString(subscription().id, resourceGroup().id, name), 6)

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${name}-${suffix}'
  location: location
  tags: tags
  properties: {
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: enablePublicNetworkAccess ? 'Enabled' : 'Disabled'
    publicNetworkAccessForQuery: enablePublicNetworkAccess ? 'Enabled' : 'Disabled'
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${name}-${suffix}'
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    publicNetworkAccessForIngestion: enablePublicNetworkAccess ? 'Enabled' : 'Disabled'
    publicNetworkAccessForQuery: enablePublicNetworkAccess ? 'Enabled' : 'Disabled'
  }
}

output workspaceId string = workspace.id
output applicationInsightsId string = applicationInsights.id
output applicationInsightsConnectionString string = applicationInsights.properties.ConnectionString
