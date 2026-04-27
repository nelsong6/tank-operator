# ============================================================================
# claude-container CI — image build from tank-operator
# ============================================================================
# The Dockerfile lives in this repo under claude-container/. Same arrangement
# as mcp_github_ci.tf: dedicated SP for the build workflow, AcrPush on the
# shared ACR (created by infra-bootstrap, referenced here as a data source),
# federated only for main.

resource "azuread_application" "claude_container_ci" {
  display_name = "claude-container-ci"
  owners       = [data.azuread_client_config.current.object_id]
}

resource "azuread_service_principal" "claude_container_ci" {
  client_id = azuread_application.claude_container_ci.client_id
  owners    = [data.azuread_client_config.current.object_id]
}

resource "azuread_application_federated_identity_credential" "claude_container_ci_main" {
  application_id = azuread_application.claude_container_ci.id
  display_name   = "tank-operator-claude-container-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:nelsong6/tank-operator:ref:refs/heads/main"
}

resource "azurerm_role_assignment" "claude_container_ci_acr_push" {
  scope                = data.azurerm_container_registry.main.id
  role_definition_name = "AcrPush"
  principal_id         = azuread_service_principal.claude_container_ci.object_id
}

resource "github_actions_variable" "claude_container_ci_client_id" {
  repository    = "tank-operator"
  variable_name = "CLAUDE_CONTAINER_CI_CLIENT_ID"
  value         = azuread_application.claude_container_ci.client_id
}
