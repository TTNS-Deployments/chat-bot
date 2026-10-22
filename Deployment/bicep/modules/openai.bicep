// Azure OpenAI account with a chat deployment, an embedding deployment, or both. Key (local) auth
// is disabled unless a Free-tier search service needs a key to call the embedding model; all
// other callers use Microsoft Entra ID with the Cognitive Services OpenAI User role.

param location string
param tags object
param name string
param sku string
param logAnalyticsWorkspaceId string
param enableDiagnostics bool
param allowKeyAccess bool

param includeChat bool
param chatDeploymentName string = 'chat'
param chatModelName string = ''
param chatModelVersion string = ''
param chatDeploymentSku string = 'GlobalStandard'
param chatCapacity int = 1

param includeEmbedding bool
param embeddingDeploymentName string = 'embedding'
param embeddingModelName string = ''
param embeddingModelVersion string = ''
param embeddingDeploymentSku string = 'GlobalStandard'
param embeddingCapacity int = 1

resource account 'Microsoft.CognitiveServices/accounts@2025-09-01' = {
  name: name
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: sku
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: !allowKeyAccess
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-09-01' = if (includeChat) {
  parent: account
  name: chatDeploymentName
  sku: {
    name: chatDeploymentSku
    capacity: chatCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModelName
      version: chatModelVersion
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}

// Deployments on one account are created one at a time; parallel creation fails.
resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-09-01' = if (includeEmbedding) {
  parent: account
  name: embeddingDeploymentName
  dependsOn: [
    chat
  ]
  sku: {
    name: embeddingDeploymentSku
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (enableDiagnostics) {
  scope: account
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

output name string = account.name
output endpoint string = account.properties.endpoint
