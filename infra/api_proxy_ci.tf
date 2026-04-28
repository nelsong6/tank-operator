# ============================================================================
# api-proxy CI — image build from tank-operator
# ============================================================================
# Dedicated SP for the build workflow, AcrPush on the shared registry,
# federated only for main. Same shape as mcp_github_ci.tf.

resource "azuread_application" "api_proxy_ci" {
  display_name = "api-proxy-ci"
  owners       = [data.azuread_client_config.current.object_id]
}

resource "azuread_service_principal" "api_proxy_ci" {
  client_id = azuread_application.api_proxy_ci.client_id
  owners    = [data.azuread_client_config.current.object_id]
}

resource "azuread_application_federated_identity_credential" "api_proxy_ci_main" {
  application_id = azuread_application.api_proxy_ci.id
  display_name   = "tank-operator-api-proxy-main"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:nelsong6/tank-operator:ref:refs/heads/main"
}

resource "azurerm_role_assignment" "api_proxy_ci_acr_push" {
  scope                = data.azurerm_container_registry.main.id
  role_definition_name = "AcrPush"
  principal_id         = azuread_service_principal.api_proxy_ci.object_id
}

resource "github_actions_variable" "api_proxy_ci_client_id" {
  repository    = "tank-operator"
  variable_name = "API_PROXY_CI_CLIENT_ID"
  value         = azuread_application.api_proxy_ci.client_id
}
