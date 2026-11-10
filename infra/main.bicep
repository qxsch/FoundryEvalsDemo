// Provisioning template for a Foundry project service.
//
// Inputs are derived from the host: azure.ai.project service body in
// azure.yaml by internal/synthesis. This entry point creates a new Foundry
// account and project; existing projects use the separate editable entry point.
//
// Subscription-scoped so the resource group is part of the deployment. This
// keeps `azd provision --preview` side-effect free: the resource group shows
// up as a previewed Create instead of being created up front to satisfy a
// resource-group-scoped what-if.

targetScope = 'subscription'

// User-defined types

@description('Shape of one model deployment entry in azure.yaml.')
type deploymentsType = deploymentType[]

@description('Shape of a single model deployment.')
type deploymentType = {
  name: string
  model: {
    name: string
    format: string
    version: string
  }
  sku: {
    name: string
    capacity: int
  }
}

type agentHostingType = {
  enabled: bool
  hostingType: 'ManagedCluster' | ''
  name: string
  clusterResourceId: string
  hostingManagementIdentityResourceId: string?
  storageAccountResourceId: string?
  workloadIdentityResourceId: string?
}

// Parameters

@description('Azure region for all resources.')
param location string

@description('Name of the resource group to create and deploy resources into.')
@minLength(1)
@maxLength(90)
param resourceGroupName string

@description('Tags applied to all resources.')
param tags object = {}

@description('Optional salt to vary resource names across re-provisions.')
param resourceTokenSalt string = ''

@description('Foundry project name. 3-32 alphanumeric/hyphen chars.')
@minLength(3)
@maxLength(32)
param foundryProjectName string

@description('Model deployments to provision on the Foundry account.')
param deployments deploymentsType = []

@description('Include an Azure Container Registry. Set true when any agent uses docker:.')
param includeAcr bool = false

@description('Existing AKS hosting with independently optional Storage and managed identity references.')
param agentHosting agentHostingType = {
  enabled: false
  hostingType: ''
  name: ''
  clusterResourceId: ''
  hostingManagementIdentityResourceId: ''
  storageAccountResourceId: ''
  workloadIdentityResourceId: ''
}

@description('AKS kubelet principal ID for registry access; resolved by microsoft.foundry when omitted.')
param kubeletObjectId string = ''

@description('Federation-management role; resolved by microsoft.foundry when omitted.')
param federatedIdentityRoleDefinitionId string = ''

@description('Full ARM ID of an existing container registry to reuse instead of creating one.')
param existingAcrResourceId string = ''

@description('Project already has effective registry pull access.')
param acrPullAssigned bool = false

@description('Unique access operation ID persisted by azd before deployment.')
param acrAccessOperationId string = ''

@description('Principal ID of the supplied hosting-management identity; resolved by microsoft.foundry when omitted.')
param hostingPrincipalId string = ''

@description('Principal ID of the supplied workload identity; resolved by microsoft.foundry when omitted.')
param workloadPrincipalId string = ''

@description('Registry pull role persisted with the access operation.')
param acrPullRoleDefinitionId string = ''

@description('Owned registry grants retained for recovery after local state loss.')
#disable-next-line no-unused-params
param acrOwnedAssignments array = []

// Network isolation parameters (see modules/resources.bicep for semantics).
// All default off so an absent network: block yields a public account.

@description('Master switch: when true the account is VNet-bound (private).')
param enableNetworkIsolation bool = false

@description('When true (and isolation on), the agent runtime uses the Microsoft-managed network instead of injecting into a customer subnet.')
param useManagedEgress bool = false

@description('ARM id of the existing customer VNet (byo mode).')
param vnetId string = ''

@description('Agent (delegated) subnet name.')
param agentSubnetName string = 'agent-subnet'

@description('Agent subnet CIDR. Empty derives a /24 from the VNet space.')
param agentSubnetPrefix string = ''

@description('When true, create the agent subnet; when false, reference it.')
param createAgentSubnet bool = false

@description('Private-endpoint subnet name.')
param peSubnetName string = 'pe-subnet'

@description('Private-endpoint subnet CIDR. Empty derives a /24 from the VNet space.')
param peSubnetPrefix string = ''

@description('When true, create the PE subnet; when false, reference it.')
param createPESubnet bool = false

@description('Managed-network isolation mode (managed mode).')
param managedIsolationMode string = ''

@description('Resource group holding existing private DNS zones. Empty creates new zones.')
param dnsZonesResourceGroup string = ''

@description('Subscription holding existing private DNS zones. Empty defaults to this subscription.')
param dnsZonesSubscription string = ''

// Demo additions (everything below is owned by this repo, not by the extension)

@description('Object id of the signed-in user (azd sets AZURE_PRINCIPAL_ID). Receives the Foundry User role so the CLI demo can create agent versions and run evals.')
param principalId string = ''

@description('Console container image. azd keeps it current through SERVICE_CONSOLE_IMAGE_NAME.')
param consoleImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Agent edited and evaluated by the console.')
param agentName string = 'contoso-te-assistant'

@description('Model deployment used as the LLM judge by the evaluators.')
param judgeModel string = 'gpt-5.4'

@description('Client id of the single-tenant Entra app registration used for console sign-in. Leave empty to skip the console.')
param entraClientId string = ''

@secure()
@description('Client secret of the Entra app registration.')
param entraClientSecret string = ''

// The console is only deployed once the Entra app registration exists.
var deployConsole = !empty(entraClientId) && !empty(entraClientSecret)

// Same token for every demo-owned resource name (log-, appi-, cr-, cae-, ca-).
var demoToken = toLower(uniqueString(subscription().id, resourceGroupName, location))

// Resources

resource resourceGroup 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources 'modules/resources.bicep' = {
  name: 'foundry-resources'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    resourceTokenSalt: resourceTokenSalt
    foundryProjectName: foundryProjectName
    deployments: deployments
    includeAcr: includeAcr
    agentHosting: agentHosting
    kubeletObjectId: kubeletObjectId
    federatedIdentityRoleDefinitionId: federatedIdentityRoleDefinitionId
    existingAcrResourceId: existingAcrResourceId
    acrPullAssigned: acrPullAssigned
    acrAccessOperationId: acrAccessOperationId
    hostingPrincipalId: hostingPrincipalId
    workloadPrincipalId: workloadPrincipalId
    acrPullRoleDefinitionId: acrPullRoleDefinitionId
    enableNetworkIsolation: enableNetworkIsolation
    useManagedEgress: useManagedEgress
    vnetId: vnetId
    agentSubnetName: agentSubnetName
    agentSubnetPrefix: agentSubnetPrefix
    createAgentSubnet: createAgentSubnet
    peSubnetName: peSubnetName
    peSubnetPrefix: peSubnetPrefix
    createPESubnet: createPESubnet
    managedIsolationMode: managedIsolationMode
    dnsZonesResourceGroup: dnsZonesResourceGroup
    dnsZonesSubscription: dnsZonesSubscription
  }
}

module userAccess 'modules/foundry-rbac.bicep' = if (!empty(principalId)) {
  name: 'foundry-user-access'
  scope: resourceGroup
  params: {
    foundryAccountName: resources.outputs.AZURE_AI_ACCOUNT_NAME
    principalId: principalId
    principalType: 'User'
  }
}

// Observability: Log Analytics + Application Insights. The Foundry project is
// connected to the App Insights component by the `azure.ai.connection` service in
// azure.yaml (the Foundry provider does not accept connection resources in Bicep).
module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    resourceToken: demoToken
  }
}

module console 'modules/console.bicep' = if (deployConsole) {
  name: 'console'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    resourceToken: demoToken
    foundryAccountName: resources.outputs.AZURE_AI_ACCOUNT_NAME
    foundryProjectEndpoint: resources.outputs.FOUNDRY_PROJECT_ENDPOINT
    agentName: agentName
    judgeModel: judgeModel
    image: consoleImage
    tenantId: tenant().tenantId
    entraClientId: entraClientId
    entraClientSecret: entraClientSecret
    logAnalyticsName: monitoring.outputs.logAnalyticsName
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
  }
}

// Outputs

output AZURE_RESOURCE_GROUP string = resourceGroupName
output AZURE_FOUNDRY_RESOURCE_GROUP string = resourceGroupName
output AZURE_AI_PROJECT_ID string = resources.outputs.AZURE_AI_PROJECT_ID
output AZURE_AI_ACCOUNT_NAME string = resources.outputs.AZURE_AI_ACCOUNT_NAME
output AZURE_AI_PROJECT_NAME string = resources.outputs.AZURE_AI_PROJECT_NAME
output AZURE_OPENAI_ENDPOINT string = resources.outputs.AZURE_OPENAI_ENDPOINT
output FOUNDRY_PROJECT_ENDPOINT string = resources.outputs.FOUNDRY_PROJECT_ENDPOINT
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = deployConsole ? console!.outputs.registryLoginServer : resources.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT
output AZURE_CONTAINER_REGISTRY_RESOURCE_ID string = resources.outputs.AZURE_CONTAINER_REGISTRY_RESOURCE_ID
output AZURE_AI_PROJECT_ACR_CONNECTION_NAME string = resources.outputs.AZURE_AI_PROJECT_ACR_CONNECTION_NAME
output AZURE_FOUNDRY_NETWORK_MODE string = resources.outputs.AZURE_FOUNDRY_NETWORK_MODE
output AZURE_FOUNDRY_MANAGED_ISOLATION_MODE string = resources.outputs.AZURE_FOUNDRY_MANAGED_ISOLATION_MODE
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString
output AZURE_APPLICATION_INSIGHTS_NAME string = monitoring.outputs.appInsightsName
output AZURE_APPLICATION_INSIGHTS_RESOURCE_ID string = monitoring.outputs.appInsightsId
output AZURE_LOG_ANALYTICS_WORKSPACE_NAME string = monitoring.outputs.logAnalyticsName
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.logAnalyticsId
output AZURE_LOG_ANALYTICS_CUSTOMER_ID string = monitoring.outputs.logAnalyticsCustomerId
output CONSOLE_URL string = deployConsole ? 'https://${console!.outputs.appFqdn}' : ''
output CONSOLE_APP_NAME string = deployConsole ? console!.outputs.appName : ''
