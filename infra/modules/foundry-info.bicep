targetScope = 'resourceGroup'

param accountName string
param deploymentNames array = []

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' existing = [for deploymentName in deploymentNames: {
  parent: account
  name: deploymentName
}]

output accountId string = account.id
output endpoint string = account.properties.endpoint
output customSubDomainName string = account.properties.customSubDomainName
output endpoints object = account.properties.endpoints
output deploymentNames array = [for (deploymentName, index) in deploymentNames: deployments[index].name]
