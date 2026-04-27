"""In-cluster OAuth gateway that fronts Claude Code's token-refresh endpoint.

Why this exists: Claude Code's subscription auth uses an OAuth refresh token
that rotates on every use (single-use refresh tokens, RFC 6749 §10.4 best
practice). When the refresh token is replicated into N ephemeral session pods,
they race each other to refresh — first pod's call rotates R1→R2, every other
pod is now holding an invalidated R1. The naive fix (copy R2 back to KV when a
pod refreshes) only papers over the race; with concurrent sessions, two pods
calling refresh in the same second still collide.

Design: there is exactly one thing in the system that ever calls
platform.claude.com's token endpoint with the real refresh token — this
gateway, running in the orchestrator pod (singleton, replicas=1). Session pods
reach it via /etc/hosts mapping platform.claude.com → this service's ClusterIP
plus a self-signed CA installed in the pod's trust bundle (NODE_EXTRA_CA_CERTS).
From claude's perspective it's still talking to Anthropic; really it's talking
to us, and we hand it a fresh access token without ever giving it the
underlying refresh token.

Single-flight: concurrent callers share one in-flight refresh against Anthropic
via an asyncio.Lock with an inside-lock cache re-check. N pods refreshing at
the same wall-clock instant → one outbound call, all N callers get the same
cached access token back.

State: the rotated refresh token is persisted to a K8s Secret on each rotation
(not back to Key Vault — KV is the cold-storage seed; the live writer is here).
The gateway re-reads the Secret on startup so a pod restart picks up where it
left off.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request
from kubernetes_asyncio import client

log = logging.getLogger(__name__)

# Hardcoded into Claude Code's bundled JS. Two distinct client_ids ship in
# the bundle: 22422756-... is paired with the legacy console.anthropic.com
# endpoint, 9d1c250a-... with platform.claude.com (our token URL). Easy to
# get wrong — both look plausible by grep alone. Tied here by the
# MANUAL_REDIRECT_URL/TOKEN_URL pairing in cli.js.
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_TOKEN_HOST = "platform.claude.com"

# What the gateway returns to in-pod claude as the refresh_token field. The
# pod will write this back to .credentials.json and POST it back to us on the
# next refresh; we ignore it and use our real refresh from the K8s Secret.
# Keeping it constant + obviously-sentinel makes accidental leaks easy to
# notice in logs.
PLACEHOLDER_REFRESH_TOKEN = "managed-by-tank-operator"

# Secret holding the JSON blob originally produced by `cat ~/.claude/.credentials.json`
# on a logged-in machine. Lives in the orchestrator namespace; session pods
# can NOT read it. Seeded once by ExternalSecret from KV; thereafter rewritten
# by this gateway on each rotation.
CREDENTIALS_NAMESPACE = os.environ.get("CLAUDE_CREDENTIALS_NAMESPACE", "tank-operator")
CREDENTIALS_SECRET = os.environ.get("CLAUDE_CREDENTIALS_SECRET", "claude-code-credentials")
CREDENTIALS_KEY = os.environ.get("CLAUDE_CREDENTIALS_KEY", "claude-code-credentials")


class OAuthGateway:
    """Single-flight OAuth refresh proxy.

    The cache holds the most recent (access_token, expires_at) tuple. On a
    request, if the cached token has > REFRESH_SKEW seconds of life, return
    it; otherwise acquire the lock, re-check (someone else may have just
    refreshed), and if still stale, perform an outbound refresh.
    """

    # Refresh REFRESH_SKEW seconds before expiry to give callers a token that
    # won't expire mid-flight. Anthropic access tokens are typically ~1h;
    # 60s headroom is plenty.
    REFRESH_SKEW = 60.0

    def __init__(self, k8s_api: client.ApiClient) -> None:
        self._core = client.CoreV1Api(k8s_api)
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._access_expires_at: float = 0.0
        # Mirrors CREDENTIALS_KEY in the Secret; loaded on first use.
        self._refresh_token: str | None = None
        # Remember the original credentials.json shape so we can preserve it
        # on rotation (field names like `accessToken`/`refreshToken` may be
        # nested under a top-level key like `claudeAiOauth` — we don't want
        # to assume the schema).
        self._credentials_blob: dict[str, Any] | None = None

    async def _load_secret(self) -> None:
        """Load the credentials JSON from the K8s Secret into memory."""
        secret = await self._core.read_namespaced_secret(
            name=CREDENTIALS_SECRET, namespace=CREDENTIALS_NAMESPACE
        )
        raw = secret.data.get(CREDENTIALS_KEY)
        if not raw:
            raise RuntimeError(
                f"secret {CREDENTIALS_NAMESPACE}/{CREDENTIALS_SECRET} missing key {CREDENTIALS_KEY}"
            )
        decoded = base64.b64decode(raw).decode("utf-8")
        blob = json.loads(decoded)
        self._credentials_blob = blob
        # Probe a few likely field paths. Claude Code's credentials.json has
        # historically been `{"claudeAiOauth": {"accessToken": ..., "refreshToken": ...}}`,
        # but this is private API and could change shape. Future-proof by
        # walking the blob for the first refresh-token-shaped field.
        self._refresh_token = _extract_refresh_token(blob)
        if not self._refresh_token:
            raise RuntimeError(
                f"could not find refresh token in {CREDENTIALS_SECRET} — JSON shape unrecognized"
            )

    async def _persist_rotation(self, new_access: str, new_refresh: str, expires_in: int) -> None:
        """Patch the credentials Secret with the rotated tokens."""
        assert self._credentials_blob is not None
        updated = _patch_credentials_blob(
            self._credentials_blob, new_access, new_refresh, expires_in
        )
        # base64-encode the JSON for the Secret data field.
        encoded = base64.b64encode(json.dumps(updated).encode("utf-8")).decode("ascii")
        await self._core.patch_namespaced_secret(
            name=CREDENTIALS_SECRET,
            namespace=CREDENTIALS_NAMESPACE,
            body={"data": {CREDENTIALS_KEY: encoded}},
        )
        self._credentials_blob = updated
        self._refresh_token = new_refresh

    async def _refresh_against_anthropic(self) -> tuple[str, int]:
        """Call platform.claude.com to refresh, persist rotation, return access+ttl."""
        assert self._refresh_token is not None
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": ANTHROPIC_CLIENT_ID,
        }
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                ANTHROPIC_TOKEN_URL,
                json=body,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            log.error(
                "oauth refresh failed: status=%s body=%s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()
        data = resp.json()
        new_access = data["access_token"]
        new_refresh = data.get("refresh_token") or self._refresh_token
        expires_in = int(data.get("expires_in", 3600))
        await self._persist_rotation(new_access, new_refresh, expires_in)
        return new_access, expires_in

    async def get_token(self) -> tuple[str, int]:
        """Return (access_token, expires_in_seconds), refreshing if needed.

        Single-flight: if multiple coroutines call concurrently while the
        cache is stale, only one performs the outbound refresh; the rest
        wait on the lock and read the freshly-cached token.
        """
        now = time.monotonic()
        if self._access_token and now < self._access_expires_at - self.REFRESH_SKEW:
            return self._access_token, int(self._access_expires_at - now)
        async with self._lock:
            now = time.monotonic()
            if self._access_token and now < self._access_expires_at - self.REFRESH_SKEW:
                return self._access_token, int(self._access_expires_at - now)
            if self._refresh_token is None:
                await self._load_secret()
            new_access, expires_in = await self._refresh_against_anthropic()
            self._access_token = new_access
            self._access_expires_at = time.monotonic() + expires_in
            return new_access, expires_in

    async def get_bootstrap_blob(self) -> dict[str, Any]:
        """Return a credentials.json blob for a fresh session pod.

        Same shape as the original .credentials.json from KV (preserves
        whatever nesting and extra fields claude expects), but with the
        access token replaced by a fresh one from the cache and the
        refresh token replaced by the placeholder. The pod's bootstrap
        writes this directly to /root/.claude/.credentials.json.
        """
        # Ensures we have a fresh access token AND that _credentials_blob
        # is populated (get_token's refresh path loads the secret).
        access_token, expires_in = await self.get_token()
        assert self._credentials_blob is not None
        return _patch_credentials_blob(
            self._credentials_blob,
            new_access=access_token,
            new_refresh=PLACEHOLDER_REFRESH_TOKEN,
            expires_in=expires_in,
        )


def _extract_refresh_token(blob: dict[str, Any]) -> str | None:
    """Walk the blob looking for the refresh token field, regardless of nesting."""
    if not isinstance(blob, dict):
        return None
    for key, value in blob.items():
        if key in ("refreshToken", "refresh_token") and isinstance(value, str):
            return value
        if isinstance(value, dict):
            found = _extract_refresh_token(value)
            if found:
                return found
    return None


def _patch_credentials_blob(
    blob: dict[str, Any], new_access: str, new_refresh: str, expires_in: int
) -> dict[str, Any]:
    """Return a copy of `blob` with access/refresh/expiry fields updated in place.

    Preserves whatever nesting structure the original blob had. We mutate
    the first occurrence we find of each known field name; the assumption
    is that .credentials.json doesn't contain multiple distinct token sets.
    """
    expires_at_ms = int((time.time() + expires_in) * 1000)
    out = json.loads(json.dumps(blob))  # cheap deep copy

    def walk(node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        patched_any = False
        for key in list(node.keys()):
            if key in ("accessToken", "access_token"):
                node[key] = new_access
                patched_any = True
            elif key in ("refreshToken", "refresh_token"):
                node[key] = new_refresh
                patched_any = True
            elif key in ("expiresAt", "expires_at"):
                node[key] = expires_at_ms
                patched_any = True
            elif isinstance(node[key], dict):
                if walk(node[key]):
                    patched_any = True
        return patched_any

    walk(out)
    return out


# --- HTTP handler ---------------------------------------------------------

# Used when the gateway has not yet been initialized in the FastAPI app's
# lifespan. The `claude_oauth_gateway` attribute is attached to the app
# instance in api.py's lifespan handler.

async def handle_oauth_token(request: Request) -> dict[str, Any]:
    """Implements the platform.claude.com /v1/oauth/token contract.

    Accepts whatever JSON body the caller sends — we ignore the
    refresh_token they provide and use our own. Returns a real access
    token (refreshed as needed) plus a placeholder refresh_token so the
    pod's .credentials.json gets rewritten with the placeholder, not a
    real refresh token.
    """
    # Defense-in-depth: the OAuth endpoint is reachable on the orchestrator's
    # public port too (same FastAPI app), so reject requests that didn't come
    # via the in-cluster Host: platform.claude.com path. The public ingress
    # always rewrites Host to tank.romaine.life.
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host != ANTHROPIC_TOKEN_HOST:
        raise HTTPException(status_code=404, detail="not found")

    # We don't even need to parse the body — the gateway's response is
    # determined by its own state, not the caller's input. But validate it
    # parses to surface bad clients clearly in logs.
    try:
        await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    gateway: OAuthGateway = request.app.state.oauth_gateway
    try:
        access_token, expires_in = await gateway.get_token()
    except Exception:
        log.exception("oauth gateway refresh failed")
        raise HTTPException(status_code=502, detail="upstream refresh failed")

    return {
        "access_token": access_token,
        "refresh_token": PLACEHOLDER_REFRESH_TOKEN,
        "expires_in": expires_in,
        "token_type": "Bearer",
    }


async def handle_bootstrap_blob(request: Request) -> dict[str, Any]:
    """Returns a complete credentials.json blob for a session pod's bootstrap.

    Same hostname gate as handle_oauth_token — only callable via the
    in-cluster TLS listener (Host: platform.claude.com), so the public
    listener returns 404. Path is namespaced under /internal/ to make it
    obvious this is not part of Anthropic's real surface.
    """
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host != ANTHROPIC_TOKEN_HOST:
        raise HTTPException(status_code=404, detail="not found")
    gateway: OAuthGateway = request.app.state.oauth_gateway
    try:
        return await gateway.get_bootstrap_blob()
    except Exception:
        log.exception("oauth gateway bootstrap blob failed")
        raise HTTPException(status_code=502, detail="upstream refresh failed")
