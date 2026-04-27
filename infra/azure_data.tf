# Shared Azure data sources used across this directory's tofu. Resources
# themselves (RG, ACR, AKS, KV) live in infra-bootstrap; tank-operator only
# reads them.

data "azuread_client_config" "current" {}

data "azurerm_client_config" "current" {}

data "azurerm_resource_group" "main" {
  name = "infra"
}

data "azurerm_key_vault" "main" {
  name                = var.key_vault_name
  resource_group_name = var.key_vault_resource_group
}

data "azurerm_container_registry" "main" {
  name                = "romainecr"
  resource_group_name = data.azurerm_resource_group.main.name
}

data "azurerm_kubernetes_cluster" "main" {
  name                = "infra-aks"
  resource_group_name = data.azurerm_resource_group.main.name
}
