// Role assignments. Every service-to-service call uses a managed identity and one of these roles;
// no service keys are stored in app settings.

param webAppPrincipalId string
param searchPrincipalId string
param deployerPrincipalId string
param deployerPrincipalType string
param openAiName string
param searchName string
param storageName string
param keyVaultName string

var roleIds = {
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  blobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  blobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
}

var hasDeployer = !empty(deployerPrincipalId)

resource openAi 'Microsoft.CognitiveServices/accounts@2025-09-01' existing = {
  name: openAiName
}

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchName
}

resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: storageName
}

resource vault 'Microsoft.KeyVault/vaults@2025-05-01' existing = {
  name: keyVaultName
}

// ---- Web app: answer questions, accept uploads, create the search index ----
resource webOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: openAi
  name: guid(openAi.id, 'web', roleIds.openAiUser)
  properties: {
    principalId: webAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.openAiUser)
  }
}

resource webSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, 'web', roleIds.searchIndexDataReader)
  properties: {
    principalId: webAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchIndexDataReader)
  }
}

resource webSearchService 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, 'web', roleIds.searchServiceContributor)
  properties: {
    principalId: webAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchServiceContributor)
  }
}

resource webBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, 'web', roleIds.blobDataContributor)
  properties: {
    principalId: webAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.blobDataContributor)
  }
}

resource webKeyVault 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, 'web', roleIds.keyVaultSecretsUser)
  properties: {
    principalId: webAppPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.keyVaultSecretsUser)
  }
}

// ---- Search service identity: the indexer reads documents and calls the embedding model ----
resource searchBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, 'search', roleIds.blobDataReader)
  properties: {
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.blobDataReader)
  }
}

resource searchOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: openAi
  name: guid(openAi.id, 'search', roleIds.openAiUser)
  properties: {
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.openAiUser)
  }
}

// ---- Deploying identity: sample/bulk ingestion from the deploy scripts, reading the upload key ----
resource deployerBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasDeployer) {
  scope: storage
  name: guid(storage.id, 'deployer', roleIds.blobDataContributor)
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.blobDataContributor)
  }
}

resource deployerSearchService 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasDeployer) {
  scope: search
  name: guid(search.id, 'deployer', roleIds.searchServiceContributor)
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchServiceContributor)
  }
}

resource deployerSearchData 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasDeployer) {
  scope: search
  name: guid(search.id, 'deployer', roleIds.searchIndexDataContributor)
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.searchIndexDataContributor)
  }
}

resource deployerOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasDeployer) {
  scope: openAi
  name: guid(openAi.id, 'deployer', roleIds.openAiUser)
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.openAiUser)
  }
}

resource deployerKeyVault 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (hasDeployer) {
  scope: vault
  name: guid(vault.id, 'deployer', roleIds.keyVaultSecretsOfficer)
  properties: {
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.keyVaultSecretsOfficer)
  }
}
