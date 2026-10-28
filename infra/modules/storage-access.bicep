targetScope = 'resourceGroup'

param storageAccountName string
param principalId string
param principalResourceId string = principalId
param artifactContainerName string
param stagingContainerName string
param stateTableName string

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = [for containerName in [artifactContainerName, stagingContainerName]: {
  parent: blobService
  name: containerName
}]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource stateTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' existing = {
  parent: tableService
  name: stateTableName
}

var blobDataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
)
var tableDataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
)

resource blobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (containerName, index) in [artifactContainerName, stagingContainerName]: {
  name: guid(containers[index].id, principalResourceId, blobDataContributorRoleId)
  scope: containers[index]
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: blobDataContributorRoleId
  }
}]

resource tableAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(stateTable.id, principalResourceId, tableDataContributorRoleId)
  scope: stateTable
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: tableDataContributorRoleId
  }
}
