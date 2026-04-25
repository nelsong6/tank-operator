terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # resource_group_name / storage_account_name / container_name / key passed by
  # the workflow via `-backend-config=` so they're not duplicated in source.
  backend "azurerm" {
    use_oidc = true
  }
}
