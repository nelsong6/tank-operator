# ============================================================================
# Anthropic API proxy — Azure side
# ============================================================================
# UAMI for the Envoy + ext_proc sidecar that fronts api.anthropic.com.
# The proxy reads the Anthropic OAuth blob from the orchestrator-namespace
# Secret (mirrored by ESO) and writes the rotated blob back to KV after
# every successful refresh.
#
# Separate from `claude-credentials-refresher-identity` (the orchestrator's
# UAMI) on least-privilege grounds: the api-proxy doesn't need any K8s
# API permissions, just KV get + set on the one secret. Decoupling SAs
# makes a future scale-out of the proxy (multiple replicas with their
# own SA) trivially safe.
# ============================================================================

resource "azurerm_user_assigned_identity" "api_proxy" {
  name                = "claude-api-proxy-identity"
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

resource "azurerm_federated_identity_credential" "api_proxy" {
  name                = "aks-claude-api-proxy"
  resource_group_name = data.azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.api_proxy.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = data.azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:tank-operator:claude-api-proxy"
}

# Same justification as credential_refresher_kv: get+set on the credentials
# secret is the entire Azure surface this identity uses, vault scope is the
# narrowest built-in role we can pick without hand-rolling a custom one.
resource "azurerm_role_assignment" "api_proxy_kv" {
  scope                = data.azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = azurerm_user_assigned_identity.api_proxy.principal_id
}

resource "azurerm_key_vault_secret" "api_proxy_client_id" {
  name         = "claude-api-proxy-mi-client-id"
  value        = azurerm_user_assigned_identity.api_proxy.client_id
  key_vault_id = data.azurerm_key_vault.main.id
}
