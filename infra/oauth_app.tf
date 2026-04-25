# OAuth2 client app reg used by oauth2-proxy in front of tank-operator's web UI.
# Distinct from the tank-operator CI Entra app (which is for tofu/ACR push from
# GitHub Actions). The CI app's SP creates this one and becomes its owner via the
# `Application.ReadWrite.OwnedBy` Graph role granted in infra-bootstrap module.app.

data "azurerm_key_vault" "kv" {
  name                = var.key_vault_name
  resource_group_name = var.key_vault_resource_group
}

resource "azuread_application" "oauth" {
  display_name     = "tank-operator-oauth"
  sign_in_audience = "AzureADMyOrg"

  web {
    redirect_uris = [
      "https://${var.hostname}/oauth2/callback",
    ]

    implicit_grant {
      access_token_issuance_enabled = false
      id_token_issuance_enabled     = false
    }
  }

  # Microsoft Graph: User.Read (delegated) is enough to read the signed-in user's
  # email/profile so oauth2-proxy can populate X-Auth-Request-Email.
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

resource "azuread_application_password" "oauth" {
  application_id    = azuread_application.oauth.id
  display_name      = "oauth2-proxy"
  end_date_relative = "8760h" # 1 year — rotate via `tofu taint` + apply
}

resource "random_password" "cookie_secret" {
  length  = 32
  special = false
}

resource "azurerm_key_vault_secret" "oauth_client_id" {
  name         = "tank-operator-oauth-client-id"
  value        = azuread_application.oauth.client_id
  key_vault_id = data.azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "oauth_client_secret" {
  name         = "tank-operator-oauth-client-secret"
  value        = azuread_application_password.oauth.value
  key_vault_id = data.azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "oauth_cookie_secret" {
  name         = "tank-operator-oauth-cookie-secret"
  value        = random_password.cookie_secret.result
  key_vault_id = data.azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "oauth_allowed_email" {
  name         = "tank-operator-oauth-allowed-email"
  value        = var.allowed_email
  key_vault_id = data.azurerm_key_vault.kv.id
}
