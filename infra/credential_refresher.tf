# ============================================================================
# Orchestrator credentials-seed UAMI — Azure side
# ============================================================================
# Provides the Azure identity the orchestrator uses to write the Anthropic
# OAuth blob to Key Vault from the in-app "+ config sub" / save-credentials
# flow (backend/src/tank_operator/credentials_seed.py). That break-glass
# harvest is now the sole consumer of this UAMI — steady-state rotation
# moved to the api-proxy's own UAMI (see infra/api_proxy.tf).
#
# (The "refresher" name is a hangover from prior designs: first a CronJob
# that owned this UAMI exclusively, then an in-process refresh loop in the
# orchestrator. Both are gone but the UAMI is reused as-is — the resource
# name is in K8s state and renaming it would force-replace the role
# assignment and the KV-published client_id for no functional benefit.)
# ============================================================================

resource "azurerm_user_assigned_identity" "credential_refresher" {
  name                = "claude-credentials-refresher-identity"
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
}

# Federated credential ties the orchestrator pod's projected SA token to
# this UAMI. Subject is system:serviceaccount:NAMESPACE:SA_NAME, matching
# the SA referenced by k8s/values.yaml's orchestrator.serviceAccount.
resource "azurerm_federated_identity_credential" "credential_refresher_orchestrator" {
  name                = "aks-tank-operator-credentials"
  resource_group_name = data.azurerm_resource_group.main.name
  parent_id           = azurerm_user_assigned_identity.credential_refresher.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = data.azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject             = "system:serviceaccount:tank-operator:tank-operator"
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
# can sync it into the orchestrator pod's AZURE_CLIENT_ID env var. Same
# pattern as infra/mcp-server/main.tf — keeps the SA → UAMI binding
# editable in one place (here) instead of duplicated in chart values.
resource "azurerm_key_vault_secret" "credential_refresher_client_id" {
  name         = "claude-credentials-refresher-mi-client-id"
  value        = azurerm_user_assigned_identity.credential_refresher.client_id
  key_vault_id = data.azurerm_key_vault.main.id
}
