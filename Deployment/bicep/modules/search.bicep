// Azure AI Search. API keys are disabled: the web app and the indexer use Microsoft Entra ID.
// The service's own managed identity reads Blob Storage and calls Azure OpenAI for embeddings.

param location string
param tags object
param name string
param sku string
param replicaCount int
param partitionCount int
param semanticSearch string
param logAnalyticsWorkspaceId string

resource service 'Microsoft.Search/searchServices@2025-05-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: sku
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    replicaCount: replicaCount
    partitionCount: partitionCount
    hostingMode: 'Default'
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true
    semanticSearch: semanticSearch
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
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
output principalId string = service.identity.principalId
