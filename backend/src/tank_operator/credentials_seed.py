"""Capture a logged-in credentials.json from a config-mode session pod and
write it to Key Vault.

This is the break-glass / first-time-setup path: when the refresh chain
dies (refresh token invalidated, e.g. because something else used it),
the user spins up a config-mode session, completes `claude /login`
interactively in the pod's terminal, and clicks "Save Credentials" —
which calls this module. From here, ESO mirrors KV → the api-proxy's
mounted Secret within ~1 minute, and the api-proxy's ext_proc sidecar
takes over rotation from the next upstream 401.

Steady-state rotation lives in the api-proxy
(api-proxy/src/tank_api_proxy/server.py). Don't call this on every
refresh — it's an interactive seeding action, not a hot path.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

from .exec_proxy import exec_capture
from .oauth_gateway import _extract_access_token, _extract_refresh_token

log = logging.getLogger(__name__)


class CredentialsSeedError(Exception):
    """Captured blob is missing fields, malformed, or KV write failed.

    Wrapped to give the API endpoint a clean error type to catch and turn
    into a 400 with a user-readable message.
    """


def _validate(blob: dict[str, Any]) -> None:
    if _extract_access_token(blob) is None:
        raise CredentialsSeedError("captured blob has no access token field")
    if _extract_refresh_token(blob) is None:
        raise CredentialsSeedError("captured blob has no refresh token field")


async def harvest_and_save(namespace: str, pod_name: str) -> None:
    """Read ~/.claude/.credentials.json out of the pod, validate, write to KV.

    Uses `sh -c` to expand $HOME so we don't hardcode the container's
    user home path. The pod is expected to be in config mode (no
    pre-seeded credentials, user has just completed `claude /login`).
    """
    raw = await exec_capture(
        namespace, pod_name, ["sh", "-c", "cat $HOME/.claude/.credentials.json"]
    )
    if not raw:
        raise CredentialsSeedError(
            "credentials file is empty or missing — complete `claude /login` "
            "in the session terminal first"
        )
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CredentialsSeedError(f"credentials.json is not valid JSON: {e}") from e
    _validate(blob)

    kv_url = os.environ["AZURE_KEYVAULT_URL"]
    secret_name = os.environ.get("CLAUDE_CREDENTIALS_KV_KEY", "claude-code-credentials")
    cred = DefaultAzureCredential()
    try:
        async with SecretClient(vault_url=kv_url, credential=cred) as kv:
            log.info("seeding %s/%s with captured credentials", kv_url, secret_name)
            await kv.set_secret(secret_name, json.dumps(blob))
    finally:
        await cred.close()


def _validate_codex(blob: dict[str, Any]) -> None:
    """Sanity-check the codex auth.json shape per developers.openai.com/codex/auth.

    We require a refresh_token because the consume-side codex CLI relies on
    it to rotate the access token in-pod; harvesting an auth.json missing
    the refresh token would leave the subscription pod with creds that
    expire in ~30min and never recover.
    """
    if blob.get("auth_mode") != "chatgpt":
        raise CredentialsSeedError(
            f"codex auth.json has auth_mode={blob.get('auth_mode')!r}; "
            "expected 'chatgpt' (the ChatGPT subscription path). "
            "Did you sign in with an API key by mistake?"
        )
    tokens = blob.get("tokens") or {}
    if not tokens.get("refresh_token"):
        raise CredentialsSeedError(
            "codex auth.json is missing tokens.refresh_token — the harvested "
            "blob would be unable to rotate. Re-run `codex login --device-auth`."
        )


async def harvest_codex_and_save(namespace: str, pod_name: str) -> None:
    """Read ~/.codex/auth.json out of a codex_config pod, validate, write to KV.

    Symmetric to harvest_and_save but for OpenAI codex's auth blob. The KV
    secret name is separate (`codex-credentials`) — codex_subscription pods
    consume a different ESO-mirrored Secret than Claude does, since they
    live in different namespaces and serve different binaries.
    """
    raw = await exec_capture(
        namespace, pod_name, ["sh", "-c", "cat $HOME/.codex/auth.json"]
    )
    if not raw:
        raise CredentialsSeedError(
            "codex auth.json is empty or missing — complete "
            "`codex login --device-auth` in the session terminal first"
        )
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CredentialsSeedError(f"auth.json is not valid JSON: {e}") from e
    _validate_codex(blob)

    kv_url = os.environ["AZURE_KEYVAULT_URL"]
    secret_name = os.environ.get("CODEX_CREDENTIALS_KV_KEY", "codex-credentials")
    cred = DefaultAzureCredential()
    try:
        async with SecretClient(vault_url=kv_url, credential=cred) as kv:
            log.info("seeding %s/%s with captured codex credentials", kv_url, secret_name)
            await kv.set_secret(secret_name, json.dumps(blob))
    finally:
        await cred.close()
