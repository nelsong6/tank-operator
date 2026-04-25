variable "arm_subscription_id" {
  description = "Azure subscription ID. Set via TF_VAR_arm_subscription_id from the workflow."
  type        = string
}

variable "arm_tenant_id" {
  description = "Entra tenant ID. Set via TF_VAR_arm_tenant_id from the workflow."
  type        = string
}

variable "key_vault_name" {
  description = "Name of the Key Vault that stores the OAuth client secret + cookie secret."
  type        = string
  default     = "romaine-kv"
}

variable "key_vault_resource_group" {
  description = "Resource group containing key_vault_name."
  type        = string
  default     = "infra"
}

variable "hostname" {
  description = "Public hostname of the tank-operator frontend; oauth2-proxy redirect URI is derived from this."
  type        = string
  default     = "tank.romaine.life"
}

variable "allowed_email" {
  description = "Email address allowed to authenticate via oauth2-proxy."
  type        = string
  default     = "nelson-devops-project@outlook.com"
}
