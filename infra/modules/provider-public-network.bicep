targetScope = 'resourceGroup'

@description('Frozen, validated writable settings read from the existing account. Never use defaults.')
param preservedAccount object
param accountName string

// Main depends on an existing-account read guard. Preflight and FullResourcePayloads
// what-if must reject Create/Replace/Delete and any non-network account change.
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: preservedAccount.location
  kind: preservedAccount.kind
  sku: preservedAccount.sku
  tags: preservedAccount.tags
  identity: preservedAccount.identity
  properties: union(preservedAccount.properties, {
    publicNetworkAccess: 'Enabled'
    networkAcls: union(preservedAccount.properties.networkAcls, {
      defaultAction: 'Allow'
      ipRules: []
    })
  })
}

output accountId string = account.id
