// AI Based Chatbot: infrastructure entry point.
//
// Compiled to Deployment/ARM/azuredeploy.json (used by the Deploy to Azure button and the
// deploy scripts) with:   Deployment/scripts/build_arm.sh
// Every tier and size is a parameter; the values per environment live in
// Deployment/ARM/parameters/<env>.parameters.json (the SKU files).

metadata description = 'AI Based Chatbot: Azure OpenAI, Azure AI Search, Blob Storage, App Service, Key Vault and monitoring. All service-to-service access uses managed identities.'

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------------------
@description('Short lowercase alphanumeric prefix for all resource names (3-8 chars).')
@minLength(3)
@maxLength(8)
param solutionPrefix string = 'aibot'

@description('Environment name, used in resource names and tags.')
@allowed([
  'dev'
  'test'
  'uat'
  'prod'
  'demo'
])
param environmentName string = 'dev'

@description('Region for all resources except Azure OpenAI (see openAiLocation).')
param location string = resourceGroup().location

@description('Extra tags applied to every resource.')
param tags object = {}

@description('Short resource names (<prefix>-web-<suffix>, <prefix>-openai-<suffix>, <prefix>-logs, ...) instead of <prefix>-<environment>-<suffix>-<type>. The demo uses them with the prefix "chatbot".')
param shortResourceNames bool = false

// ---------------------------------------------------------------------------------------
// Azure OpenAI
// ---------------------------------------------------------------------------------------
@description('Region for the Azure OpenAI account. Must offer both models with the chosen deployment types. East US 2 offers the default models with every deployment type.')
param openAiLocation string = 'eastus2'

@description('Azure OpenAI account tier.')
@allowed([
  'S0'
])
param openAiSku string = 'S0'

@description('Chat model that writes the answers.')
param chatModelName string = 'gpt-5-mini'

@description('Chat model version.')
param chatModelVersion string = '2025-08-07'

@description('Reasoning effort sent to reasoning models such as gpt-5-mini (lower is faster and cheaper). Use none for models that do not reason.')
@allowed([
  'none'
  'minimal'
  'low'
  'medium'
  'high'
])
param chatReasoningEffort string = 'low'

@description('Chat deployment type. Standard keeps processing in openAiLocation; DataZoneStandard and GlobalStandard may process prompts in other regions.')
@allowed([
  'GlobalStandard'
  'DataZoneStandard'
  'Standard'
])
param chatDeploymentSku string = 'GlobalStandard'

@description('Chat deployment capacity, in thousands of tokens per minute (TPM).')
@minValue(1)
@maxValue(1000)
param chatCapacity int = 30

@description('Region for the embedding model. Empty means the same Azure OpenAI account and region as the chat model; another region creates a second Azure OpenAI account there.')
param embeddingLocation string = ''

@description('Embedding model used to vectorise document chunks and questions. The index vector size follows from it (3-small: 1536, 3-large: 3072).')
@allowed([
  'text-embedding-3-small'
  'text-embedding-3-large'
])
param embeddingModelName string = 'text-embedding-3-small'

@description('Embedding model version.')
param embeddingModelVersion string = '1'

@description('Embedding deployment type.')
@allowed([
  'GlobalStandard'
  'DataZoneStandard'
  'Standard'
])
param embeddingDeploymentSku string = 'GlobalStandard'

@description('Embedding deployment capacity, in thousands of tokens per minute (TPM). Limits how fast documents are indexed.')
@minValue(1)
@maxValue(1000)
param embeddingCapacity int = 30

// ---------------------------------------------------------------------------------------
// Azure AI Search
// ---------------------------------------------------------------------------------------
@description('Azure AI Search tier. free: one per subscription, 50 MB, no semantic ranker, and no managed identity (keys in Key Vault are used instead).')
@allowed([
  'free'
  'basic'
  'standard'
  'standard2'
  'standard3'
])
param searchSku string = 'basic'

@description('Search replicas (query throughput and availability). basic: 1-3, standard tiers: 1-12. Two or more for the read SLA.')
@minValue(1)
@maxValue(12)
param searchReplicaCount int = 1

@description('Search partitions (index size). basic: 1-3, standard tiers: 1, 2, 3, 4, 6 or 12.')
@allowed([
  1
  2
  3
  4
  6
  12
])
param searchPartitionCount int = 1

@description('Semantic ranker plan. free: 1,000 semantic queries a month; standard: pay per 1,000 queries; disabled: hybrid search without reranking.')
@allowed([
  'disabled'
  'free'
  'standard'
])
param semanticSearch string = 'free'

// ---------------------------------------------------------------------------------------
// Storage, Key Vault, monitoring
// ---------------------------------------------------------------------------------------
@description('Storage redundancy for the source documents.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
  'Standard_GRS'
  'Standard_RAGRS'
  'Standard_GZRS'
  'Standard_RAGZRS'
])
param storageSku string = 'Standard_LRS'

@description('Key Vault tier.')
@allowed([
  'standard'
  'premium'
])
param keyVaultSku string = 'standard'

@description('Log Analytics retention in days.')
@minValue(30)
@maxValue(730)
param logAnalyticsRetentionDays int = 30

@description('Send diagnostic logs from App Service, AI Search and Azure OpenAI to Log Analytics. Application Insights is always on.')
param enableDiagnostics bool = true

@description('Allow shared-key (account key) access to the storage account. Needed when deploying code from Azure Cloud Shell (run-from-package upload). A Free-tier search service turns it on anyway.')
param allowStorageSharedKey bool = false

@description('Log Analytics daily ingestion cap in GB (-1 = no cap).')
@minValue(-1)
@maxValue(1000)
param logAnalyticsDailyQuotaGb int = 1

// ---------------------------------------------------------------------------------------
// App Service
// ---------------------------------------------------------------------------------------
@description('App Service plan size (Linux).')
@allowed([
  'B1'
  'B2'
  'B3'
  'S1'
  'S2'
  'S3'
  'P0v3'
  'P1v3'
  'P2v3'
  'P3v3'
])
param appServicePlanSku string = 'B1'

@description('Number of web app instances.')
@minValue(1)
@maxValue(10)
param appServiceInstanceCount int = 1

@description('Name of an existing Linux App Service plan in this resource group and region to put the web app on. Empty creates a new plan from appServicePlanSku. An existing plan is never changed.')
param existingAppServicePlanName string = ''

// ---------------------------------------------------------------------------------------
// Application
// ---------------------------------------------------------------------------------------
@description('Deploy the application code from a public Git repository during the deployment (used by the Deploy to Azure button). The deploy scripts upload the code themselves and leave this off.')
param deployAppCode bool = false

@description('Public Git repository that holds the application code (used when deployAppCode is true).')
param repositoryUrl string = 'https://github.com/TTNS-Deployments/chat-bot'

@description('Branch to deploy from repositoryUrl.')
param repositoryBranch string = 'main'

@description('Let users add documents from the chat page. Uploads always require the upload key stored in Key Vault.')
param allowDocumentUpload bool = true

@description('Key that protects document upload, stored in Key Vault as upload-api-key. Leave empty on a redeploy to keep the stored key.')
@secure()
param uploadApiKey string = ''

@description('Who can open the chat page. none: anyone with the address. password: a shared demo password (no Microsoft Entra ID app registration needed).')
@allowed([
  'none'
  'password'
])
param authMode string = 'none'

@description('Shared password for authMode password, stored in Key Vault as demo-password. Leave empty on a redeploy to keep the stored password.')
@secure()
param demoPassword string = ''

@description('URL (with SAS) of a run-from-package zip that includes the dependencies. Set by the Cloud Shell code deployment so that infrastructure redeploys keep the running code.')
@secure()
param appPackageUrl string = ''

// ---------------------------------------------------------------------------------------
// Deploying identity (set by the deploy scripts)
// ---------------------------------------------------------------------------------------
@description('Object ID of the user or service principal running the deployment. Gets data access for document ingestion. Leave empty to skip.')
param deployerPrincipalId string = ''

@description('Principal type of deployerPrincipalId.')
@allowed([
  'User'
  'ServicePrincipal'
])
param deployerPrincipalType string = 'User'


// ---------------------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------------------
var prefix = toLower(solutionPrefix)
var suffix = take(uniqueString(resourceGroup().id, solutionPrefix, environmentName), 6)
var baseName = '${prefix}-${environmentName}-${suffix}'
var allTags = union(tags, {
  solution: 'ai-based-chatbot'
  environment: environmentName
})

// Names that become web addresses (web app, Azure OpenAI, search, Key Vault, storage) must be unique
// across Azure, so they keep the suffix in both styles.
var names = shortResourceNames ? {
  logAnalytics: '${prefix}-logs'
  appInsights: '${prefix}-insights'
  storage: take('${prefix}st${suffix}', 24)
  keyVault: take('${prefix}-kv-${suffix}', 24)
  search: '${prefix}-search-${suffix}'
  openAi: '${prefix}-openai-${suffix}'
  embedding: '${prefix}-embed-${suffix}'
  plan: '${prefix}-plan-${suffix}'
  webApp: '${prefix}-web-${suffix}'
} : {
  logAnalytics: '${baseName}-log'
  appInsights: '${baseName}-appi'
  storage: take('${prefix}${environmentName}${suffix}st', 24)
  keyVault: take('${prefix}-kv-${suffix}', 24)
  search: '${baseName}-srch'
  openAi: '${baseName}-aoai'
  embedding: '${baseName}-embed'
  plan: '${baseName}-web-plan'
  webApp: '${baseName}-web'
}

var documentsContainer = 'documents'
var searchIndexName = 'documents-index'
var chatDeploymentName = 'chat'
var embeddingDeploymentName = 'embedding'
var embeddingDimensions = embeddingModelName == 'text-embedding-3-large' ? 3072 : 1536

// A Free search service has no managed identity, so it (and the web app calling it) use keys.
var searchUsesKeys = searchSku == 'free'
var semanticEnabled = !searchUsesKeys && semanticSearch != 'disabled'
var separateEmbedding = !empty(embeddingLocation) && embeddingLocation != openAiLocation
var embeddingAccountName = separateEmbedding ? names.embedding : names.openAi
var keyVaultRef = 'VaultName=${names.keyVault}'

// ---------------------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------------------
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    tags: allTags
    logAnalyticsName: names.logAnalytics
    appInsightsName: names.appInsights
    retentionDays: logAnalyticsRetentionDays
    dailyQuotaGb: logAnalyticsDailyQuotaGb
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    tags: allTags
    name: names.storage
    sku: storageSku
    containerName: documentsContainer
    allowSharedKeyAccess: allowStorageSharedKey || searchUsesKeys
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    location: location
    tags: allTags
    name: names.keyVault
    sku: keyVaultSku
    uploadApiKey: uploadApiKey
    demoPassword: demoPassword
  }
}

module openAi 'modules/openai.bicep' = {
  name: 'openai'
  params: {
    location: openAiLocation
    tags: allTags
    name: names.openAi
    sku: openAiSku
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    enableDiagnostics: enableDiagnostics
    allowKeyAccess: searchUsesKeys && !separateEmbedding
    includeChat: true
    chatDeploymentName: chatDeploymentName
    chatModelName: chatModelName
    chatModelVersion: chatModelVersion
    chatDeploymentSku: chatDeploymentSku
    chatCapacity: chatCapacity
    includeEmbedding: !separateEmbedding
    embeddingDeploymentName: embeddingDeploymentName
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingDeploymentSku: embeddingDeploymentSku
    embeddingCapacity: embeddingCapacity
  }
}

module embeddingOpenAi 'modules/openai.bicep' = if (separateEmbedding) {
  name: 'openai-embedding'
  params: {
    location: embeddingLocation
    tags: allTags
    name: names.embedding
    sku: openAiSku
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    enableDiagnostics: enableDiagnostics
    allowKeyAccess: searchUsesKeys
    includeChat: false
    includeEmbedding: true
    embeddingDeploymentName: embeddingDeploymentName
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingDeploymentSku: embeddingDeploymentSku
    embeddingCapacity: embeddingCapacity
  }
}

module search 'modules/search.bicep' = {
  name: 'search'
  params: {
    location: location
    tags: allTags
    name: names.search
    sku: searchSku
    replicaCount: searchReplicaCount
    partitionCount: searchPartitionCount
    semanticSearch: semanticSearch
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    enableDiagnostics: enableDiagnostics
  }
}

module searchKeys 'modules/searchkeys.bicep' = if (searchUsesKeys) {
  name: 'search-keys'
  dependsOn: [
    keyVault
    storage
    search
    openAi
    embeddingOpenAi
  ]
  params: {
    keyVaultName: names.keyVault
    searchName: names.search
    storageName: names.storage
    embeddingAccountName: embeddingAccountName
  }
}

var embeddingEndpoint = separateEmbedding ? 'https://${names.embedding}.openai.azure.com/' : openAi.outputs.endpoint

var coreSettings = {
  CHATBOT_MODE: 'azure'
  AZURE_OPENAI_ENDPOINT: openAi.outputs.endpoint
  AZURE_OPENAI_API_VERSION: '2025-04-01-preview'
  AZURE_OPENAI_CHAT_DEPLOYMENT: chatDeploymentName
  CHAT_REASONING_EFFORT: chatReasoningEffort
  AZURE_OPENAI_EMBEDDING_ENDPOINT: embeddingEndpoint
  AZURE_OPENAI_EMBEDDING_DEPLOYMENT: embeddingDeploymentName
  AZURE_OPENAI_EMBEDDING_MODEL: embeddingModelName
  AZURE_OPENAI_EMBEDDING_DIMENSIONS: string(embeddingDimensions)
  AZURE_SEARCH_ENDPOINT: search.outputs.endpoint
  AZURE_SEARCH_INDEX: searchIndexName
  AZURE_SEARCH_SEMANTIC_CONFIG: semanticEnabled ? 'default' : ''
  AZURE_STORAGE_ACCOUNT_URL: storage.outputs.blobEndpoint
  AZURE_STORAGE_CONTAINER: documentsContainer
  AZURE_STORAGE_RESOURCE_ID: storage.outputs.id
  ALLOW_DOCUMENT_UPLOAD: string(allowDocumentUpload)
  UPLOAD_API_KEY: '@Microsoft.KeyVault(${keyVaultRef};SecretName=upload-api-key)'
  AUTH_MODE: authMode
}

var passwordSettings = authMode == 'password' ? {
  DEMO_PASSWORD: '@Microsoft.KeyVault(${keyVaultRef};SecretName=demo-password)'
} : {}

var searchKeySettings = searchUsesKeys ? {
  AZURE_SEARCH_API_KEY: '@Microsoft.KeyVault(${keyVaultRef};SecretName=search-admin-key)'
  AZURE_SEARCH_DATASOURCE_CONNECTION: '@Microsoft.KeyVault(${keyVaultRef};SecretName=search-storage-connection)'
  AZURE_OPENAI_EMBEDDING_KEY: '@Microsoft.KeyVault(${keyVaultRef};SecretName=search-embedding-key)'
} : {}

// Run-from-package: the zip already contains the dependencies, so App Service must not build.
var packageSettings = empty(appPackageUrl) ? {} : {
  WEBSITE_RUN_FROM_PACKAGE: appPackageUrl
  SCM_DO_BUILD_DURING_DEPLOYMENT: 'false'
  PYTHONPATH: '/home/site/wwwroot/.packages'
}

module web 'modules/appservice.bicep' = {
  name: 'appservice'
  params: {
    location: location
    tags: allTags
    planName: names.plan
    existingPlanName: existingAppServicePlanName
    webAppName: names.webApp
    planSku: appServicePlanSku
    instanceCount: appServiceInstanceCount
    appInsightsName: monitoring.outputs.appInsightsName
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    enableDiagnostics: enableDiagnostics
    deployAppCode: deployAppCode
    repositoryUrl: repositoryUrl
    repositoryBranch: repositoryBranch
    appSettings: union(coreSettings, passwordSettings, searchKeySettings, packageSettings)
  }
}

module roles 'modules/roles.bicep' = {
  name: 'roles'
  dependsOn: [
    embeddingOpenAi
  ]
  params: {
    webAppPrincipalId: web.outputs.principalId
    searchPrincipalId: search.outputs.principalId
    deployerPrincipalId: deployerPrincipalId
    deployerPrincipalType: deployerPrincipalType
    openAiName: openAi.outputs.name
    embeddingAccountName: embeddingAccountName
    searchName: search.outputs.name
    storageName: storage.outputs.name
    keyVaultName: keyVault.outputs.name
    searchUsesManagedIdentity: !searchUsesKeys
  }
}

// ---------------------------------------------------------------------------------------
// Outputs (read by the deploy scripts)
// ---------------------------------------------------------------------------------------
output webAppName string = web.outputs.name
output webAppUrl string = 'https://${web.outputs.defaultHostName}'
output storageAccountName string = storage.outputs.name
output storageBlobEndpoint string = storage.outputs.blobEndpoint
output documentsContainer string = documentsContainer
output searchServiceName string = search.outputs.name
output searchEndpoint string = search.outputs.endpoint
output searchIndexName string = searchIndexName
output searchIndexerName string = '${searchIndexName}-indexer'
output searchUsesKeys bool = searchUsesKeys
output openAiEndpoint string = openAi.outputs.endpoint
output embeddingEndpoint string = embeddingEndpoint
output chatDeployment string = chatDeploymentName
output embeddingDeployment string = embeddingDeploymentName
output embeddingDimensions int = embeddingDimensions
output keyVaultName string = keyVault.outputs.name
output appInsightsName string = monitoring.outputs.appInsightsName
output authMode string = authMode
