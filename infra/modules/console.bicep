// Demo console: a Container App (Entra ID sign-in via built-in authentication)
// that edits the Foundry agent and triggers evaluations. Talks to Foundry with
// a user-assigned managed identity that holds the "Foundry User" role.

targetScope = 'resourceGroup'

@description('Azure region.')
param location string = resourceGroup().location

@description('Tags applied to all resources.')
param tags object = {}

@description('Short unique token used in resource names.')
@minLength(3)
@maxLength(16)
param resourceToken string

@description('Name of the Foundry (AIServices) account the console talks to.')
param foundryAccountName string

@description('Foundry project endpoint, https://<account>.services.ai.azure.com/api/projects/<project>.')
param foundryProjectEndpoint string

@description('Agent edited / evaluated by the console.')
param agentName string

@description('Deployment used as LLM judge by evaluators.')
param judgeModel string

@description('Container image. azd replaces it on deploy.')
param image string

@description('Entra tenant that is allowed to sign in (single-tenant).')
param tenantId string

@description('Client id of the Entra app registration used for sign-in.')
param entraClientId string

@secure()
@description('Client secret of the Entra app registration.')
param entraClientSecret string

@description('Name of the shared Log Analytics workspace (monitoring.bicep) that receives the container logs.')
param logAnalyticsName string

@secure()
@description('Application Insights connection string; the console exports its telemetry there.')
param appInsightsConnectionString string

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'cr${resourceToken}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-console-${resourceToken}'
  location: location
  tags: tags
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

module foundryAccess 'foundry-rbac.bicep' = {
  name: 'console-foundry-access'
  params: {
    foundryAccountName: foundryAccountName
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource containerEnv 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'ca-console-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'console' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'entra-client-secret'
          value: entraClientSecret
        }
        {
          name: 'appinsights-connection-string'
          value: appInsightsConnectionString
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'console'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
            { name: 'AGENT_NAME', value: agentName }
            { name: 'JUDGE_MODEL', value: judgeModel }
            { name: 'AUTH_MODE', value: 'easyauth' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-connection-string' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    acrPull
    foundryAccess
  ]
}

// Sign-in with Entra ID; every unauthenticated request is redirected to the
// single-tenant app registration, so any user of this tenant can use the console.
resource auth 'Microsoft.App/containerApps/authConfigs@2025-01-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: [
        '/healthz'
      ]
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
          clientId: entraClientId
          clientSecretSettingName: 'entra-client-secret'
        }
        validation: {
          allowedAudiences: [
            'api://${entraClientId}'
            entraClientId
          ]
        }
      }
    }
    login: {
      preserveUrlFragmentsForLogins: false
    }
  }
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output appName string = app.name
output appFqdn string = app.properties.configuration.ingress.fqdn
output environmentDefaultDomain string = containerEnv.properties.defaultDomain
output identityPrincipalId string = identity.properties.principalId
