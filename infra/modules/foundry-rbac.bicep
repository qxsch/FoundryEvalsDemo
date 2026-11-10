// Grants the "Foundry User" built-in role (data-plane access to agents,
// evaluations, and model calls) on the Foundry account to one principal.

targetScope = 'resourceGroup'

@description('Name of the existing Foundry (AIServices) account.')
param foundryAccountName string

@description('Object id of the user / service principal / managed identity.')
param principalId string

@allowed(['User', 'ServicePrincipal', 'Group'])
param principalType string = 'User'

// Foundry User (formerly "Azure AI User")
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, foundryUserRoleId)
  scope: account
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
  }
}
