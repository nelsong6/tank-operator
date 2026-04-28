"""Helpers for parsing/patching the Anthropic credentials.json blob.

History: this module used to contain a stateless OAuth-gateway HTTP
handler that impersonated platform.claude.com for in-cluster session
pods. That gateway has been retired in favor of the api-proxy in front
of api.anthropic.com (see api-proxy/src/tank_api_proxy/server.py): pods
no longer attempt to refresh because we seed them with a far-future
expiresAt, and the proxy injects the real Bearer on every request.

What's left here are pure helpers consumed by credentials_seed.py
(the "+ config sub" break-glass harvest): walk a credentials.json blob,
find the access/refresh token fields regardless of nesting. The
patch helper is unused for now but kept alongside its inverses so a
future caller (e.g. if we ever resurrect the gateway) lands the whole
parser shape in one file.
"""
from __future__ import annotations

import json
from typing import Any


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
