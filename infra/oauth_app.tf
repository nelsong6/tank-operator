# Entra app reg for the tank-operator web UI sign-in flow.
#
# Public SPA + the kill-me/microsoft-routes pattern: the browser uses MSAL.js
# to obtain an Entra ID token, POSTs it to /api/auth/microsoft/login, the
# backend validates it via JWKS and mints its own short-lived JWT for the
# remainder of the session. No confidential client secret needed.
#
# Distinct from the tank-operator CI Entra app (which is for tofu/ACR push
# from GitHub Actions). The CI app's SP creates this one and becomes its
# owner; without the explicit `owners` attribute, the azuread provider
# doesn't record ownership and `Application.ReadWrite.OwnedBy` returns 403
# on any follow-up Graph call.

data "azurerm_key_vault" "kv" {
  name                = var.key_vault_name
  resource_group_name = var.key_vault_resource_group
}

data "azuread_client_config" "current" {}

resource "azuread_application" "oauth" {
  display_name = "tank-operator-oauth"
  # Personal MSA accounts (e.g. outlook.com) need this; AzureADMyOrg-only apps
  # rejected by the consumer auth flow with `unauthorized_client`. Sign-in is
  # still gated by the backend's ALLOWED_EMAIL allowlist.
  sign_in_audience = "AzureADandPersonalMicrosoftAccount"

  owners = [data.azuread_client_config.current.object_id]

  # v2 access tokens are required when sign_in_audience includes
  # PersonalMicrosoftAccount; absent this block, tofu refuses to plan.
  api {
    requested_access_token_version = 2
  }

  # SPA platform — MSAL.js auth-code-with-PKCE flow, no client secret.
  single_page_application {
    redirect_uris = [
      "https://${var.hostname}/",
    ]
  }

  # Microsoft Graph: User.Read (delegated) is enough for MSAL to fetch the
  # signed-in user's profile (email, name) for the ID token claims.
  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000"

    resource_access {
      id   = "e1fe6dd8-ba31-4d61-89e7-88639da4683d" # User.Read
      type = "Scope"
    }
  }
}

resource "azuread_service_principal" "oauth" {
  client_id = azuread_application.oauth.client_id
}

# Self-signed JWT secret used by the backend to mint per-session tokens after
# verifying the Entra ID token. Single secret, rotate by tainting and applying;
# tainting invalidates all live sessions, which is fine for a single-user tool.
resource "random_password" "jwt_secret" {
  length  = 64
  special = false
}

resource "azurerm_key_vault_secret" "oauth_client_id" {
  name         = "tank-operator-oauth-client-id"
  value        = azuread_application.oauth.client_id
  key_vault_id = data.azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "jwt_secret" {
  name         = "tank-operator-jwt-secret"
  value        = random_password.jwt_secret.result
  key_vault_id = data.azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "oauth_allowed_email" {
  name         = "tank-operator-oauth-allowed-email"
  value        = var.allowed_email
  key_vault_id = data.azurerm_key_vault.kv.id
}
