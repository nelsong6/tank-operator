# ============================================================================
# MCP (Model Context Protocol) Servers
# ============================================================================
# Each MCP server runs in-cluster as its own pod with its own dedicated
# managed identity. Clients (session pods) authenticate to the server using
# their projected K8s SA token; a kube-rbac-proxy sidecar in the chart
# performs the TokenReview. Upstream Azure permissions live on the server's
# UAMI — anyone authenticated to the server inherits them, by design.
#
# Per-server resources (UAMI, federated credential, role assignments,
# KV-published client ID) live in the ./mcp-server module. Helm charts that
# consume the KV-published client IDs live in k8s-mcp-azure/ and
# k8s-mcp-github/.
# ============================================================================

# Tenant ID — not secret, but kept in KV so MCP ExternalSecrets can pull it
# alongside per-server IDs without anything having to know tenant specifics
# statically.
resource "azurerm_key_vault_secret" "mcp_tenant_id" {
  name         = "mcp-tenant-id"
  value        = data.azurerm_client_config.current.tenant_id
  key_vault_id = data.azurerm_key_vault.main.id
}

# ----------------------------------------------------------------------------
# Per-server: azure
# ----------------------------------------------------------------------------
# Hosts Microsoft's azure-mcp. The UAMI gets Reader at subscription scope —
# read-only across the sub gives every read tool azure-mcp ships, with no
# write paths. Promote to a tighter scope or a different role as the
# surface narrows.

module "mcp_azure" {
  source = "./mcp-server"

  name                     = "azure"
  resource_group_name      = data.azurerm_resource_group.main.name
  resource_group_location  = data.azurerm_resource_group.main.location
  key_vault_id             = data.azurerm_key_vault.main.id
  aks_oidc_issuer_url      = data.azurerm_kubernetes_cluster.main.oidc_issuer_url
  aks_namespace            = "mcp-azure"
  aks_service_account_name = "mcp-azure"

  role_assignments = {
    "subscription-reader" = {
      scope                = "/subscriptions/${data.azurerm_client_config.current.subscription_id}"
      role_definition_name = "Reader"
    }
    # KV data-plane access. Reader at the control plane gives us the
    # vault's metadata but NOT secret/cert reads — those need this
    # data-plane role. Without it any caller-driven KV operation through
    # azure-mcp comes back as auth failure (the SDK rolls 403 on
    # getSecret into the same ChainedTokenCredential error path it uses
    # for missing tokens).
    "kv-secrets-user" = {
      scope                = data.azurerm_key_vault.main.id
      role_definition_name = "Key Vault Secrets User"
    }
  }
}
