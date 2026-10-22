// Azure AI Search. On Basic and higher, API keys are disabled: the web app uses Microsoft Entra ID,
// and the service's own managed identity reads Blob Storage and calls Azure OpenAI for embeddings.
// The Free tier supports neither a managed identity nor the semantic ranker, so it uses keys
// (kept in Key Vault, see searchkeys.bicep) and no semantic configuration.

param location string
param tags object
param name string
param sku string
param replicaCount int
param partitionCount int
param semanticSearch string
param logAnalyticsWorkspaceId string
param enableDiagnostics bool

var isFree = sku == 'free'

resource service 'Microsoft.Search/searchServices@2025-05-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: sku
  }
  identity: {
    type: isFree ? 'None' : 'SystemAssigned'
  }
  properties: {
    replicaCount: isFree ? 1 : replicaCount
    partitionCount: isFree ? 1 : partitionCount
    hostingMode: 'Default'
    publicNetworkAccess: 'enabled'
    disableLocalAuth: !isFree
    semanticSearch: isFree ? null : semanticSearch
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (enableDiagnostics) {
  scope: service
  name: 'to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'allLogs'
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

output name string = service.name
output endpoint string = 'https://${service.name}.search.windows.net'
output principalId string = isFree ? '' : service.identity.principalId
