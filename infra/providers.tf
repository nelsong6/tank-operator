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

# Repo-scoped Actions variables require admin perms that the default
# GITHUB_TOKEN doesn't have. Use the same `github-pat` PAT in KV that
# infra-bootstrap uses; the workflow fetches it and exports it as
# TF_VAR_github_pat.
provider "github" {
  owner = "nelsong6"
  token = var.github_pat
}
