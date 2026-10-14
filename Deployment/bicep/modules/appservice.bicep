// Linux App Service plan and the chatbot web app (FastAPI on gunicorn/uvicorn, Python 3.12),
// with a system-assigned managed identity. Optionally pulls the code from a public Git repository.

param location string
param tags object
param planName string
param webAppName string
param planSku string
param instanceCount int
param appInsightsName string
param logAnalyticsWorkspaceId string
param deployAppCode bool
param repositoryUrl string
param repositoryBranch string

@description('App settings specific to the chatbot (endpoints and names).')
param appSettings object

var tiers = {
  B1: 'Basic'
  B2: 'Basic'
  B3: 'Basic'
  S1: 'Standard'
  S2: 'Standard'
  S3: 'Standard'
  P0v3: 'Premium0V3'
  P1v3: 'PremiumV3'
  P2v3: 'PremiumV3'
  P3v3: 'PremiumV3'
}

// Same command for code deployed by the scripts (zip) and from Git: both keep the repository layout.
var startupCommand = 'gunicorn --chdir Code/backend --bind=0.0.0.0:8000 --timeout 120 --workers 2 --worker-class uvicorn.workers.UvicornWorker chatbot.main:app'

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource plan 'Microsoft.Web/serverfarms@2025-03-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: planSku
    tier: tiers[planSku]
    capacity: instanceCount
  }
  properties: {
    reserved: true
  }
}

var platformSettings = {
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
  SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
}

resource site 'Microsoft.Web/sites@2025-03-01' = {
  name: webAppName
  location: location
  tags: union(tags, {
    'azd-service-name': 'web'
  })
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: startupCommand
      alwaysOn: true
      http20Enabled: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      healthCheckPath: '/healthz'
    }
  }
}

resource settings 'Microsoft.Web/sites/config@2025-03-01' = {
  parent: site
  name: 'appsettings'
  properties: union(platformSettings, appSettings)
}

resource ftpCredentials 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2025-03-01' = {
  parent: site
  name: 'ftp'
  properties: {
    allow: false
  }
}

// The Git deployment must start after the app settings exist (it builds with SCM_DO_BUILD_DURING_DEPLOYMENT).
resource sourceControl 'Microsoft.Web/sites/sourcecontrols@2025-03-01' = if (deployAppCode) {
  parent: site
  name: 'web'
  dependsOn: [
    settings
  ]
  properties: {
    repoUrl: repositoryUrl
    branch: repositoryBranch
    isManualIntegration: true
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: site
  name: 'to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'AppServiceHTTPLogs'
        enabled: true
      }
      {
        category: 'AppServiceConsoleLogs'
        enabled: true
      }
      {
        category: 'AppServiceAppLogs'
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

output name string = site.name
output defaultHostName string = site.properties.defaultHostName
output principalId string = site.identity.principalId
