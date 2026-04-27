# ============================================================================
# mcp-github CI — image build from tank-operator
# ============================================================================
# Dedicated SP for the build workflow, AcrPush on the shared registry,
# federated only for main. Same shape as claude_container_ci.tf.

resource "azuread_application" "mcp_github_ci" {
  display_name = "mcp-github-ci"
  owners       = [data.azuread_client_config.current.object_id]
}

resource "azuread_service_principal" "mcp_github_ci" {
  client_id = azuread_application.mcp_github_ci.client_id
  owners    = [data.azuread_client_config.current.object_id]
}

resource "azuread_application_federated_identity_credential" "mcp_github_ci_main" {
  application_id = azuread_application.mcp_github_ci.id
  display_name   = "tank-operator-mcp-github-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:nelsong6/tank-operator:ref:refs/heads/main"
}

resource "azurerm_role_assignment" "mcp_github_ci_acr_push" {
  scope                = data.azurerm_container_registry.main.id
  role_definition_name = "AcrPush"
  principal_id         = azuread_service_principal.mcp_github_ci.object_id
}

resource "github_actions_variable" "mcp_github_ci_client_id" {
  repository    = "tank-operator"
  variable_name = "MCP_GITHUB_CI_CLIENT_ID"
  value         = azuread_application.mcp_github_ci.client_id
}
