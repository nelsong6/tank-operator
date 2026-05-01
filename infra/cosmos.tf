# ============================================================================
# Cosmos DB — profile store for multi-user (#57)
# ============================================================================
# Per-user profile rows live here, keyed by email. Each profile carries the
# user's GitHub App installation_id so mcp-github (#57 stage 3) can mint a
# per-caller installation token instead of every user sharing the host's.
#
# Why Cosmos and not Postgres: relational isn't load-bearing for the profile
# shape (one document per user, no joins) and Azure managed Postgres is
# overkill for the volume. Cosmos serverless gives pay-per-RU pricing that
# rounds to zero at this scale, no patch-window operations, and the same
# workload-identity auth path the orchestrator already uses for KV writes.
#
# Why reuse the credential_refresher UAMI instead of a dedicated one: that
# UAMI is already federated to the orchestrator SA and is the only Azure
# identity the orchestrator process holds. Adding a Cosmos role to it doesn't
# change the orchestrator's blast radius — the process is the trust boundary,
# not the UAMI. Same calculus as the existing KV role on this UAMI.
# ============================================================================

variable "cosmos_account_name" {
  description = "Globally unique Cosmos DB account name. Override via TF_VAR_cosmos_account_name if the default is taken."
  type        = string
  default     = "tank-operator-romaine"
}

resource "azurerm_cosmosdb_account" "tank_operator" {
  name                = var.cosmos_account_name
  resource_group_name = data.azurerm_resource_group.main.name
  location            = data.azurerm_resource_group.main.location
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  # Serverless billing — pay-per-RU, no provisioned throughput. Profile reads
  # are sub-RU per request and writes are bursty (one per first-login per
  # user), so monthly cost rounds to zero at this volume.
  capabilities {
    name = "EnableServerless"
  }

  consistency_policy {
    # Session consistency is the default and matches the access pattern:
    # writers (login + install callback) read their own writes, no
    # cross-region replication concerns.
    consistency_level = "Session"
  }

  # Single region to match the rest of the stack. Profile data is small and
  # the orchestrator is single-replica; multi-region replication adds cost
  # and consistency complexity for no current benefit.
  geo_location {
    location          = data.azurerm_resource_group.main.location
    failover_priority = 0
  }

  # Workload-identity-only auth path. Local (key-based) auth would be a
  # parallel credential surface we'd have to rotate; turning it off forces
  # every caller through Entra + RBAC.
  local_authentication_disabled = true
}

resource "azurerm_cosmosdb_sql_database" "tank_operator" {
  name                = "tank-operator"
  resource_group_name = data.azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.tank_operator.name
}

resource "azurerm_cosmosdb_sql_container" "profiles" {
  name                = "profiles"
  resource_group_name = data.azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.tank_operator.name
  database_name       = azurerm_cosmosdb_sql_database.tank_operator.name
  partition_key_paths = ["/email"]

  # Email serves as both id and partition key — single-document upsert/get
  # is one logical RU, no cross-partition queries needed for the profile
  # access pattern.
  indexing_policy {
    indexing_mode = "consistent"
    included_path { path = "/*" }
  }
}

# Cosmos DB Built-in Data Contributor — read + write items in the database
# (data plane only, no schema mutation). The well-known role definition id
# is universal across Cosmos accounts; the scoped reference below pins it
# to this account's role-definitions subresource.
resource "azurerm_cosmosdb_sql_role_assignment" "orchestrator_profiles" {
  resource_group_name = data.azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.tank_operator.name
  role_definition_id  = "${azurerm_cosmosdb_account.tank_operator.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_user_assigned_identity.credential_refresher.principal_id
  scope               = azurerm_cosmosdb_account.tank_operator.id
}
