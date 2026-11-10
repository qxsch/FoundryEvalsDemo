// Observability backbone shared by the whole demo: one Log Analytics workspace that
// stores the telemetry of a workspace-based Application Insights component. The
// Foundry project is connected to the component (see foundry-appinsights.bicep) so
// agent runs are traced, and the console's Container Apps environment logs into the
// same workspace.

targetScope = 'resourceGroup'

@description('Azure region.')
param location string = resourceGroup().location

@description('Tags applied to all resources.')
param tags object = {}

@description('Short unique token used in resource names.')
@minLength(3)
@maxLength(16)
param resourceToken string

@description('Days to retain telemetry.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: retentionInDays
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output logAnalyticsName string = logAnalytics.name
output logAnalyticsId string = logAnalytics.id
output logAnalyticsCustomerId string = logAnalytics.properties.customerId
output appInsightsName string = appInsights.name
output appInsightsId string = appInsights.id
output appInsightsAppId string = appInsights.properties.AppId
output appInsightsConnectionString string = appInsights.properties.ConnectionString
