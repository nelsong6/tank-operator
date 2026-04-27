# ============================================================================
# Credential refresher — Azure side
# ============================================================================
# A small CronJob in the cluster (k8s/templates/credential-refresher.yaml)
# rotates the Anthropic OAuth credentials in Key Vault on a fixed schedule.
# This is the ONLY thing in the system with write access to the
# `claude-code-credentials` secret; the orchestrator pod just reads the
# ESO-mirrored copy. See backend/src/tank_operator/refresh_credentials.py
# for the rotation logic.
#
# Why a separate UAMI (vs. reusing the orchestrator's): the orchestrator
# has no Azure surface today and shouldn't grow one. Keeping the writer
# isolated to its own identity means a bug in the orchestrator can't
# accidentally touch KV, and tightening the scope later is a one-line
# change here instead of a multi-system audit.
# ============================================================================

resource "azurerm_user_assigned_identity" "credential_refresher" {
  name                = "claude-credentials-refresher-identity"
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

# Federated credential ties the AKS-projected SA token to this UAMI.
# Subject is system:serviceaccount:NAMESPACE:SA_NAME, matching the SA
# created by the Helm chart in the orchestrator namespace.
resource "azurerm_federated_identity_credential" "credential_refresher" {
  name                = "aks-claude-credentials-refresher"
  resource_group_name = data.azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.credential_refresher.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = data.azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:tank-operator:claude-credentials-refresher"
}

# `Key Vault Secrets Officer` covers get + set + list + delete on secrets.
# We only need get + set, but there's no narrower built-in role and a
# custom role is overkill for a one-secret writer. Scope is the entire
# vault rather than the specific secret because (a) KV scope-to-secret
# requires the secret to already exist as a separate Azure resource,
# coupling apply order, and (b) this UAMI has no other Azure surface
# anyway, so vault-wide vs. secret-scoped is the same blast radius.
resource "azurerm_role_assignment" "credential_refresher_kv" {
  scope                = data.azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = azurerm_user_assigned_identity.credential_refresher.principal_id
}

# Publish the UAMI's client_id to KV so the Helm chart's ExternalSecret
# can sync it into the CronJob pod's AZURE_CLIENT_ID env var. Same
# pattern as infra/mcp-server/main.tf — keeps the SA → UAMI binding
# editable in one place (here) instead of duplicated in chart values.
resource "azurerm_key_vault_secret" "credential_refresher_client_id" {
  name         = "claude-credentials-refresher-mi-client-id"
  value        = azurerm_user_assigned_identity.credential_refresher.client_id
  key_vault_id = data.azurerm_key_vault.main.id
}
