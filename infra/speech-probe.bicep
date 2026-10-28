targetScope = 'subscription'

@minLength(1)
@description('Environment name used for deterministic resource naming and tags.')
param environmentName string

@minLength(1)
@description('Explicit existing dedicated resource group. This template never creates or retags it.')
param resourceGroupName string

@description('Primary region for managed OpenAI transcription deployments.')
param primaryLocation string = 'swedencentral'

@description('Region where MAI transcription is available through Azure Speech.')
param maiLocation string = 'eastus'

@minLength(3)
@description('Canonical bounded IPv4 CIDR allowed to call the probe endpoints.')
param operatorIpCidr string

@minLength(36)
@maxLength(36)
@description('Microsoft Entra object ID of the user running the compatibility probe.')
param operatorPrincipalId string

@description('Optional tags applied to probe resources only; resource-group tags are never changed.')
param tags object = {}

var resourceTags = union(tags, {
  'azd-env-name': environmentName
  'ai-bench-purpose': 'speech-compatibility-probe'
})
var suffix = take(uniqueString(subscription().id, resourceGroupName), 8)

resource probeResourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: resourceGroupName
}

module transcriptionResources './modules/speech-probe-resources.bicep' = {
  name: 'speech-probe-resources-${suffix}'
  scope: probeResourceGroup
  params: {
    name: 'stt-${suffix}'
    location: primaryLocation
    maiLocation: maiLocation
    operatorIpCidr: operatorIpCidr
    operatorPrincipalId: operatorPrincipalId
    tags: resourceTags
  }
}

output AZURE_RESOURCE_GROUP string = probeResourceGroup.name
output AI_BENCH_TRANSCRIPTION_ACCOUNT string = transcriptionResources.outputs.primaryAccountName
output AI_BENCH_TRANSCRIPTION_ENDPOINT string = transcriptionResources.outputs.primaryEndpoint
output AI_BENCH_MAI_ACCOUNT string = transcriptionResources.outputs.maiAccountName
output AI_BENCH_MAI_ENDPOINT string = transcriptionResources.outputs.maiEndpoint
output AI_BENCH_GPT_TRANSCRIBE_DEPLOYMENT string = transcriptionResources.outputs.gptTranscribeDeployment
output AI_BENCH_WHISPER_DEPLOYMENT string = transcriptionResources.outputs.whisperDeployment
