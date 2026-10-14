// Key Vault (RBAC mode). Holds the document upload key; the web app reads it through a
// Key Vault reference with its managed identity.

param location string
param tags object
param name string
param sku string

@secure()
param uploadApiKey string

var uploadKeySecretName = 'upload-api-key'

resource vault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: sku
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource uploadKey 'Microsoft.KeyVault/vaults/secrets@2025-05-01' = {
  parent: vault
  name: uploadKeySecretName
  properties: {
    value: uploadApiKey
    contentType: 'Key for POST /api/documents (header X-Upload-Key)'
  }
}

output name string = vault.name
output uploadKeySecretName string = uploadKey.name
