targetScope = 'resourceGroup'

param appName string

@secure()
param existingAppSettings object

@secure()
param managedAppSettings object

resource app 'Microsoft.Web/sites@2023-12-01' existing = {
  name: appName
}

resource appSettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: app
  name: 'appsettings'
  properties: union(existingAppSettings, managedAppSettings)
}
