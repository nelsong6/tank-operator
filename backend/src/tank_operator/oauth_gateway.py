"""In-cluster OAuth gateway that fronts Claude Code's token-refresh endpoint.

Why this exists: Claude Code's subscription auth uses an OAuth refresh token
that rotates on every use (single-use refresh tokens, RFC 6749 §10.4 best
practice). The real refresh token must never enter session pods — if it did,
N pods would race to refresh and invalidate each other.

Design: this gateway is a stateless reader. The credentials.json blob lives
in a K8s Secret (synced from Key Vault by ExternalSecrets), mounted into
this pod as a file. Session pods reach the gateway via /etc/hosts mapping
platform.claude.com → this service's ClusterIP plus a self-signed CA
installed in their trust bundle. From claude's perspective it's still
talking to Anthropic; really it's talking to us, and we hand it the current
access token without ever giving it the refresh token.

Where rotation happens: a separate CronJob (see refresh_credentials.py)
is the *only* writer of the credentials. It reads the current refresh
token from KV, calls platform.claude.com, writes the rotated blob back to
KV. ESO mirrors KV → K8s Secret → kubelet → this pod's mounted file.
The gateway never makes outbound calls.

Why split rotation from reading: ESO treats KV as the source of truth, so
any in-process write here would race against ESO's reconciliation. Splitting
the writer (CronJob, scheduled, singleton via concurrencyPolicy:Forbid) from
the readers (gateway replicas, stateless) makes the data flow one-way:
KV → K8s Secret → mounted file → HTTP response. No locks, no caches, safe
to scale out.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

ANTHROPIC_TOKEN_HOST = "platform.claude.com"

# What the gateway returns to in-pod claude as the refresh_token field. The
# pod will write this back to .credentials.json and POST it back to us on the
# next refresh; we ignore it and read the real one from the mounted file.
# Keeping it constant + obviously-sentinel makes accidental leaks easy to
# notice in logs.
PLACEHOLDER_REFRESH_TOKEN = "managed-by-tank-operator"

# Path to the credentials.json blob mounted from the K8s Secret. Kubelet
# atomically swaps the file when the Secret changes (via symlink rename),
# so a concurrent read either sees the old blob or the new one — never a
# torn write. We re-open + re-read on every request rather than caching to
# pick up rotations within seconds of ESO syncing them in.
CREDENTIALS_FILE = os.environ.get(
    "CLAUDE_CREDENTIALS_FILE", "/etc/claude-credentials/credentials.json"
)

# Default access-token TTL stamped into responses when the blob doesn't
# carry an expiry. claude only uses this hint for cache invalidation; the
# next request re-reads the file anyway, so an over-estimate just means
# claude trusts a stale token a bit longer before re-asking us.
DEFAULT_EXPIRES_IN_SECONDS = 1800


def _load_blob() -> dict[str, Any]:
    """Read and parse the mounted credentials.json. Raises on any error."""
    with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_access_token(blob: dict[str, Any]) -> str | None:
    """Walk the blob looking for the access token field, regardless of nesting."""
    if not isinstance(blob, dict):
        return None
    for key, value in blob.items():
        if key in ("accessToken", "access_token") and isinstance(value, str):
            return value
        if isinstance(value, dict):
            found = _extract_access_token(value)
            if found:
                return found
    return None


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
    import time

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


# --- HTTP handlers --------------------------------------------------------


async def handle_oauth_token(request: Request) -> dict[str, Any]:
    """Implements the platform.claude.com /v1/oauth/token contract.

    Accepts whatever JSON body the caller sends — we ignore the
    refresh_token they provide and use our own. Returns the current access
    token plus a placeholder refresh_token so the pod's .credentials.json
    gets rewritten with the placeholder, not a real refresh token.
    """
    # Defense-in-depth: the OAuth endpoint is reachable on the orchestrator's
    # public port too (same FastAPI app), so reject requests that didn't come
    # via the in-cluster Host: platform.claude.com path. The public ingress
    # always rewrites Host to tank.romaine.life.
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host != ANTHROPIC_TOKEN_HOST:
        raise HTTPException(status_code=404, detail="not found")

    # We don't even need to parse the body — the response is determined by
    # the mounted file, not the caller's input. But validate it parses to
    # surface bad clients clearly in logs.
    try:
        await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    try:
        blob = _load_blob()
        access_token = _extract_access_token(blob)
    except Exception:
        log.exception("oauth gateway: could not read credentials file")
        raise HTTPException(status_code=502, detail="credentials unavailable")
    if not access_token:
        log.error("oauth gateway: credentials blob has no access token field")
        raise HTTPException(status_code=502, detail="credentials malformed")

    return {
        "access_token": access_token,
        "refresh_token": PLACEHOLDER_REFRESH_TOKEN,
        "expires_in": DEFAULT_EXPIRES_IN_SECONDS,
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

    try:
        blob = _load_blob()
        access_token = _extract_access_token(blob)
    except Exception:
        log.exception("oauth gateway: could not read credentials file")
        raise HTTPException(status_code=502, detail="credentials unavailable")
    if not access_token:
        log.error("oauth gateway: credentials blob has no access token field")
        raise HTTPException(status_code=502, detail="credentials malformed")

    return _patch_credentials_blob(
        blob,
        new_access=access_token,
        new_refresh=PLACEHOLDER_REFRESH_TOKEN,
        expires_in=DEFAULT_EXPIRES_IN_SECONDS,
    )
