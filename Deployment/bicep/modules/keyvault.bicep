// Key Vault (RBAC mode). Holds the administrator key for deleting documents and, for demo sign-in, the demo password.
// The web app reads them through Key Vault references with its managed identity.
// A secret is written only when a value is passed, so a redeploy without it keeps the stored one.

param location string
param tags object
param name string
param sku string

@secure()
param uploadApiKey string

@secure()
param demoPassword string

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

resource uploadKey 'Microsoft.KeyVault/vaults/secrets@2025-05-01' = if (!empty(uploadApiKey)) {
  parent: vault
  name: 'upload-api-key'
  properties: {
    value: uploadApiKey
    contentType: 'Key for DELETE /api/documents (header X-Upload-Key)'
  }
}

resource password 'Microsoft.KeyVault/vaults/secrets@2025-05-01' = if (!empty(demoPassword)) {
  parent: vault
  name: 'demo-password'
  properties: {
    value: demoPassword
    contentType: 'Shared password for the demo sign-in page'
  }
}

output name string = vault.name
