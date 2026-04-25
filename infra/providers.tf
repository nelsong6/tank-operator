provider "azurerm" {
  features {}
  use_oidc        = true
  subscription_id = var.arm_subscription_id
  tenant_id       = var.arm_tenant_id
}

provider "azuread" {
  use_oidc  = true
  tenant_id = var.arm_tenant_id
}

provider "random" {}
