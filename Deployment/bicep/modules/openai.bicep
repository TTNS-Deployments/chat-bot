// Azure OpenAI account with a chat and an embedding deployment. Key (local) auth is disabled:
// callers use Microsoft Entra ID with the Cognitive Services OpenAI User role.

param location string
param tags object
param name string
param sku string
param logAnalyticsWorkspaceId string

param chatDeploymentName string
param chatModelName string
param chatModelVersion string
param chatDeploymentSku string
param chatCapacity int

param embeddingDeploymentName string
param embeddingModelName string
param embeddingModelVersion string
param embeddingDeploymentSku string
param embeddingCapacity int

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
    disableLocalAuth: true
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2025-09-01' = {
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
resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2025-09-01' = {
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

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
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
