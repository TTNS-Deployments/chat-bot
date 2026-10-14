// Log Analytics workspace and workspace-based Application Insights.

param location string
param tags object
param logAnalyticsName string
param appInsightsName string
param retentionDays int
param dailyQuotaGb int

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionDays
    workspaceCapping: {
      dailyQuotaGb: dailyQuotaGb
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

output logAnalyticsWorkspaceId string = workspace.id
output appInsightsName string = appInsights.name
