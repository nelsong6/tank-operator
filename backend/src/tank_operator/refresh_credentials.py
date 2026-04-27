"""Rotate Anthropic OAuth credentials in Key Vault.

Reads the current credentials.json blob from Key Vault, calls
platform.claude.com with the refresh token to get a fresh access+refresh
pair, writes the rotated blob back to Key Vault. ExternalSecrets then
mirrors KV to the in-cluster Secret that the OAuth gateway pod mounts as
a file.

Driven by the orchestrator's session lifecycle (see sessions.py): kicked
on the 0 → 1 session transition, runs periodically while sessions exist,
stops when the last session leaves. Idle clusters do zero rotations —
the refresh token sits in KV untouched until the next user session.

This is the only thing in the system that calls platform.claude.com's
token endpoint with the real refresh token. The gateway is read-only;
session pods get a placeholder refresh token so they can't refresh
themselves and invalidate ours.

Failure modes:
  - Anthropic returns 400 invalid_grant: the refresh token in KV is no
    longer valid (someone re-authenticated elsewhere, or a previous
    rotation succeeded but didn't persist). User runs the in-app
    "+ config sub" flow to re-seed.
  - Anthropic transient 5xx / network: the orchestrator's periodic loop
    retries on the next interval; in-flight sessions keep using the
    last known-good access token until then.
  - KV write fails after a successful Anthropic refresh: WORST CASE.
    Anthropic has rotated R1→R2, but KV still holds R1. Next call will
    400. Recovery: re-seed via "+ config sub". The network can't be
    rolled back, accept the risk.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

from .oauth_gateway import _extract_refresh_token, _patch_credentials_blob

log = logging.getLogger(__name__)

# Hardcoded into Claude Code's bundled JS. Two distinct client_ids ship in
# the bundle: 22422756-... is paired with the legacy console.anthropic.com
# endpoint, 9d1c250a-... with platform.claude.com (our token URL). Tied
# here by the MANUAL_REDIRECT_URL/TOKEN_URL pairing in cli.js.
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"


async def refresh_now() -> None:
    """One rotation: read KV, refresh against Anthropic, write KV back."""
    kv_url = os.environ["AZURE_KEYVAULT_URL"]
    secret_name = os.environ.get("CLAUDE_CREDENTIALS_KV_KEY", "claude-code-credentials")

    # DefaultAzureCredential picks up the workload-identity env vars +
    # token file the AKS webhook injects when the pod's SA is annotated.
    cred = DefaultAzureCredential()
    try:
        async with SecretClient(vault_url=kv_url, credential=cred) as kv:
            log.info("loading current credentials from %s/%s", kv_url, secret_name)
            current = await kv.get_secret(secret_name)
            blob = json.loads(current.value or "")
            refresh_token = _extract_refresh_token(blob)
            if not refresh_token:
                raise RuntimeError(
                    f"KV secret {secret_name} has no refresh token field — "
                    "re-seed with a fresh credentials.json from a logged-in machine"
                )

            log.info("calling %s to rotate", ANTHROPIC_TOKEN_URL)
            async with httpx.AsyncClient(timeout=30.0) as http:
                resp = await http.post(
                    ANTHROPIC_TOKEN_URL,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": ANTHROPIC_CLIENT_ID,
                    },
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code != 200:
                log.error("oauth refresh failed: status=%s body=%s", resp.status_code, resp.text[:500])
                resp.raise_for_status()
            data: dict[str, Any] = resp.json()

            new_access = data["access_token"]
            # Anthropic always rotates the refresh token; falling back to
            # the old one preserves continuity if a future server
            # implementation stops including it in the response.
            new_refresh = data.get("refresh_token") or refresh_token
            expires_in = int(data.get("expires_in", 3600))

            updated = _patch_credentials_blob(blob, new_access, new_refresh, expires_in)
            log.info("writing rotated blob back to KV (access expires in %ds)", expires_in)
            await kv.set_secret(secret_name, json.dumps(updated))
    finally:
        await cred.close()
