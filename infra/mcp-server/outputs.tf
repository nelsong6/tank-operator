output "managed_identity_client_id" {
  value       = azurerm_user_assigned_identity.mcp.client_id
  description = "Client ID of the MCP server's UAMI."
}

output "managed_identity_principal_id" {
  value       = azurerm_user_assigned_identity.mcp.principal_id
  description = "Principal ID of the MCP server's UAMI."
}
