// Only for a Free-tier search service, which cannot use a managed identity. Copies the keys it
// needs into Key Vault, where the web app reads them through Key Vault references:
// - search-admin-key: the web app calls the search service with it
// - search-storage-connection: the indexer reads the documents container with it
// - search-embedding-key: the indexer and the index vectorizer call the embedding model with it

param keyVaultName string
param searchName string
param storageName string
param embeddingAccountName string

resource vault 'Microsoft.KeyVault/vaults@2025-05-01' existing = {
  name: keyVaultName
}

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchName
}

resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: storageName
}

resource embeddingAccount 'Microsoft.CognitiveServices/accounts@2025-09-01' existing = {
  name: embeddingAccountName
}

resource searchKey 'Microsoft.KeyVault/vaults/secrets@2025-05-01' = {
  parent: vault
  name: 'search-admin-key'
  properties: {
    value: search.listAdminKeys().primaryKey
  }
}

resource storageConnection 'Microsoft.KeyVault/vaults/secrets@2025-05-01' = {
  parent: vault
  name: 'search-storage-connection'
  properties: {
    value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
  }
}

resource embeddingKey 'Microsoft.KeyVault/vaults/secrets@2025-05-01' = {
  parent: vault
  name: 'search-embedding-key'
  properties: {
    value: embeddingAccount.listKeys().key1
  }
}

output searchKeySecret string = searchKey.name
output storageConnectionSecret string = storageConnection.name
output embeddingKeySecret string = embeddingKey.name
