"""Per-user profile storage in Cosmos DB.

Document shape (id == email == partition key, all lowercased):

    {
      "id": "user@example.com",
      "email": "user@example.com",
      "github_login": null,
      "installation_id": null,
      "created_at": "<iso8601>",
      "updated_at": "<iso8601>"
    }

Auto-created on first login when /api/auth/microsoft/login mints a session
JWT for an allowed email. The multi-user GitHub App flow (#57 stage 2)
populates installation_id from the install callback; mcp-github multi-tenancy
(#57 stage 3) reads it to mint a per-caller installation token.

Auth: workload identity. The orchestrator pod's azure.workload.identity/use
label causes the WI webhook to inject a federated SA-token at
AZURE_FEDERATED_TOKEN_FILE; DefaultAzureCredential picks it up alongside
AZURE_CLIENT_ID + AZURE_TENANT_ID env vars (already set for the existing
KV write path) and exchanges for an Entra access token. The Cosmos account
has local_authentication_disabled = true, so this is the only auth path.
"""

from __future__ import annotations

import datetime
import logging
import os
from dataclasses import asdict, dataclass

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential

log = logging.getLogger(__name__)

COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT", "")
COSMOS_DATABASE = os.environ.get("COSMOS_DATABASE", "tank-operator")
COSMOS_PROFILES_CONTAINER = os.environ.get("COSMOS_PROFILES_CONTAINER", "profiles")


@dataclass
class Profile:
    email: str
    github_login: str | None = None
    installation_id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _profile_from_doc(doc: dict) -> Profile:
    return Profile(
        email=doc["email"],
        github_login=doc.get("github_login"),
        installation_id=doc.get("installation_id"),
        created_at=doc.get("created_at", ""),
        updated_at=doc.get("updated_at", ""),
    )


class ProfileStore:
    """Async Cosmos client wrapper for the profiles container.

    `_enabled` gates the whole class on COSMOS_ENDPOINT being set, so a
    cluster install where tofu hasn't yet provisioned Cosmos boots without
    crash-looping — get_or_create returns a stub Profile in that case.
    Once the env var lands, behavior switches to the real store on next
    pod restart. This is the same degraded-mode pattern sessions.py uses
    for unresolved Service hostnames.
    """

    def __init__(self) -> None:
        self._credential: DefaultAzureCredential | None = None
        self._client: CosmosClient | None = None
        self._container = None  # azure.cosmos.aio.ContainerProxy
        self._enabled = bool(COSMOS_ENDPOINT)

    async def startup(self) -> None:
        if not self._enabled:
            log.warning(
                "COSMOS_ENDPOINT unset; profile storage disabled "
                "(stub Profiles will be returned)"
            )
            return
        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(COSMOS_ENDPOINT, credential=self._credential)
        database = self._client.get_database_client(COSMOS_DATABASE)
        self._container = database.get_container_client(COSMOS_PROFILES_CONTAINER)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._credential is not None:
            await self._credential.close()

    async def get_or_create(self, email: str) -> Profile:
        """Return the profile for `email`, creating an empty row if missing.

        Called on /api/auth/microsoft/login so a profile row exists before
        any feature that needs one (install callback, multi-tenant
        mcp-github) tries to read it.
        """
        normalized = email.lower()
        if not self._enabled or self._container is None:
            return Profile(email=normalized)
        try:
            doc = await self._container.read_item(
                item=normalized, partition_key=normalized
            )
            return _profile_from_doc(doc)
        except CosmosResourceNotFoundError:
            now = _now_iso()
            doc = {
                "id": normalized,
                "email": normalized,
                "github_login": None,
                "installation_id": None,
                "created_at": now,
                "updated_at": now,
            }
            await self._container.create_item(body=doc)
            return _profile_from_doc(doc)

    async def get(self, email: str) -> Profile:
        """Return the profile for `email`. Equivalent to get_or_create today;
        kept as a separate name so future code that should never auto-create
        (e.g. install callback, which expects the row to already exist from
        login) can bind to a method that signals that intent.
        """
        return await self.get_or_create(email)

    async def update_installation(
        self, email: str, installation_id: int, github_login: str | None
    ) -> Profile:
        """Set the profile's GitHub App installation. Used by the install
        callback (#57 stage 2).

        Tolerates a missing row — if the user hits the callback without a
        prior login (stale tab, manual URL), we create the profile rather
        than 500. The state JWT verified by the caller is the auth anchor.
        """
        normalized = email.lower()
        if not self._enabled or self._container is None:
            return Profile(
                email=normalized,
                installation_id=installation_id,
                github_login=github_login,
            )
        now = _now_iso()
        try:
            doc = await self._container.read_item(
                item=normalized, partition_key=normalized
            )
        except CosmosResourceNotFoundError:
            doc = {
                "id": normalized,
                "email": normalized,
                "created_at": now,
            }
        doc["installation_id"] = installation_id
        doc["github_login"] = github_login
        doc["updated_at"] = now
        await self._container.upsert_item(body=doc)
        return _profile_from_doc(doc)
