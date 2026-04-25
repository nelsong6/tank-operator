output "oauth_client_id" {
  description = "Client ID of the tank-operator-oauth Entra app reg."
  value       = azuread_application.oauth.client_id
}

output "oauth_app_object_id" {
  description = "Object ID of the tank-operator-oauth Entra app reg."
  value       = azuread_application.oauth.object_id
}

output "redirect_uri" {
  description = "Configured OAuth2 redirect URI."
  value       = "https://${var.hostname}/oauth2/callback"
}
