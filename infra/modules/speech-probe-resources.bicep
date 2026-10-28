targetScope = 'resourceGroup'

@minLength(1)
param name string

param location string = resourceGroup().location
param maiLocation string
@minLength(3)
param operatorIpCidr string
param operatorPrincipalId string
param tags object = {}

var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var primaryAccountName = 'ais-${name}'
var maiAccountName = 'ais-${name}-mai'
var operatorIpRule = endsWith(operatorIpCidr, '/32')
  ? substring(operatorIpCidr, 0, length(operatorIpCidr) - 3)
  : operatorIpCidr

resource primaryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: primaryAccountName
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: primaryAccountName
    disableLocalAuth: true
    dynamicThrottlingEnabled: true
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      ipRules: [
        {
          value: operatorIpRule
        }
      ]
      virtualNetworkRules: []
    }
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: true
  }
  tags: tags
}

resource gptTranscribe 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  name: 'gpt-transcribe'
  parent: primaryAccount
  sku: {
    capacity: 1
    name: 'GlobalStandard'
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-transcribe'
      version: '2026-07-28'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [
    whisper
  ]
}

resource whisper 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  name: 'whisper'
  parent: primaryAccount
  sku: {
    capacity: 1
    name: 'Standard'
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'whisper'
      version: '001'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource primaryOperatorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(primaryAccount.id, operatorPrincipalId, cognitiveServicesUserRoleId)
  scope: primaryAccount
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesUserRoleId
    )
  }
}

resource maiAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: maiAccountName
  location: maiLocation
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: maiAccountName
    disableLocalAuth: true
    dynamicThrottlingEnabled: true
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      ipRules: [
        {
          value: operatorIpRule
        }
      ]
      virtualNetworkRules: []
    }
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: true
  }
  tags: tags
}

resource maiOperatorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(maiAccount.id, operatorPrincipalId, cognitiveServicesUserRoleId)
  scope: maiAccount
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesUserRoleId
    )
  }
}

output primaryAccountName string = primaryAccount.name
output primaryEndpoint string = primaryAccount.properties.endpoint
output maiAccountName string = maiAccount.name
output maiEndpoint string = maiAccount.properties.endpoint
output gptTranscribeDeployment string = gptTranscribe.name
output whisperDeployment string = whisper.name
