output "oauth_client_id" {
  description = "Client ID of the tank-operator-oauth Entra app reg."
  value       = azuread_application.oauth.client_id
}

output "oauth_app_object_id" {
  description = "Object ID of the tank-operator-oauth Entra app reg."
  value       = azuread_application.oauth.object_id
}

output "redirect_uri" {
  description = "MSAL.js SPA redirect URI registered on the Entra app."
  value       = "https://${var.hostname}/"
}
