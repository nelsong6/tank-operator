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

# `owner` set explicitly so github_actions_variable resources land on the
# right org. Auth is via GITHUB_TOKEN env var (the workflow exposes the
# default GITHUB_TOKEN, which has actions:write on this repo).
provider "github" {
  owner = "nelsong6"
}
