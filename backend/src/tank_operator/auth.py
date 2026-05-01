"""Auth flow mirroring the platform pattern (kill-me/microsoft-routes.js).

1. Frontend uses MSAL.js to obtain an Entra ID token.
2. Frontend POSTs it to /api/auth/microsoft/login.
3. Backend validates the ID token via JWKS using the SPA client-id as audience.
4. Backend mints its own short-lived JWT (signed with JWT_SECRET) and returns
   it in the response body and as an httpOnly cookie. The cookie covers the
   WebSocket exec endpoint where Authorization headers can't be set.
5. Subsequent calls go through `current_user` which verifies the self-signed
   JWT off the Authorization header (REST) or `auth_token` cookie (WebSocket).

Allowed-email gating is done once at /api/auth/microsoft/login — only emails
in the configured ALLOWED_EMAILS allowlist get a session token; everyone else
gets 403.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Cookie, HTTPException, Header, WebSocket
from jwt import PyJWKClient

ENTRA_CLIENT_ID = os.environ.get("ENTRA_CLIENT_ID", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
ALLOWED_EMAILS = frozenset(
    e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()
)

# Match kill-me's posture: `common` JWKS endpoint, regex issuer match. Lets
# any Microsoft user attempt to sign in; the email allowlist is the gate.
_JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
_ENTRA_ISSUER_PATTERN = re.compile(r"^https://login\.microsoftonline\.com/.+/v2\.0$")
_jwks_client = PyJWKClient(_JWKS_URL, cache_keys=True, lifespan=3600)

SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days, matches kill-me
COOKIE_NAME = "auth_token"
ALGORITHM_HS = "HS256"
ALGORITHM_RS = "RS256"

# Short-lived JWT used as the OAuth-style `state` parameter on GitHub App
# install redirects (#57 stage 2). Bound to the caller's email and a custom
# audience so it can't be cross-used as a session token. 10-minute window
# is enough for "click install → grant on GitHub → redirect back" without
# leaving a stale token usable on a future install attempt.
INSTALL_STATE_TTL_SECONDS = 10 * 60
INSTALL_STATE_AUDIENCE = "tank-operator/github-install"


def mint_install_state(email: str) -> str:
    """Sign a short-lived state token binding the install flow to `email`.

    Verified in /api/github/install/callback to reject installs that didn't
    originate from a session we just minted state for.
    """
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    now = int(time.time())
    return jwt.encode(
        {
            "email": email.lower(),
            "aud": INSTALL_STATE_AUDIENCE,
            "iat": now,
            "exp": now + INSTALL_STATE_TTL_SECONDS,
        },
        JWT_SECRET,
        algorithm=ALGORITHM_HS,
    )


def verify_install_state(state: str) -> str:
    """Verify an install-state token and return the email it was minted for."""
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    try:
        payload = jwt.decode(
            state,
            JWT_SECRET,
            algorithms=[ALGORITHM_HS],
            audience=INSTALL_STATE_AUDIENCE,
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=400, detail=f"invalid install state: {e}") from e
    email = str(payload.get("email", "")).lower()
    if not email:
        raise HTTPException(status_code=400, detail="install state has no email")
    return email


@dataclass
class User:
    sub: str
    email: str
    name: str


def _verify_entra_id_token(id_token: str) -> dict[str, Any]:
    """Validate an Entra ID token signature, audience, and issuer."""
    if not ENTRA_CLIENT_ID:
        raise HTTPException(status_code=500, detail="ENTRA_CLIENT_ID not configured")

    signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
    try:
        payload = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=[ALGORITHM_RS],
            audience=ENTRA_CLIENT_ID,
            options={"verify_iss": False},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid Entra token: {e}") from e

    iss = payload.get("iss", "")
    if not _ENTRA_ISSUER_PATTERN.match(iss):
        raise HTTPException(status_code=401, detail=f"unexpected token issuer: {iss}")
    return payload


async def exchange_microsoft_token(id_token: str) -> tuple[str, User]:
    """Verify an Entra ID token and mint a backend session JWT for the allowed user.

    Returns (session_token, user). Raises 401/403 on rejection.
    """
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    if not ALLOWED_EMAILS:
        raise HTTPException(status_code=500, detail="ALLOWED_EMAILS not configured")

    # Offload the (sync, network-bound) JWKS fetch + verify so we don't block the loop.
    payload = await asyncio.to_thread(_verify_entra_id_token, id_token)

    email = (payload.get("email") or payload.get("preferred_username") or "").lower()
    if not email:
        raise HTTPException(status_code=401, detail="token has no email or preferred_username claim")
    if email not in ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="email not allowed")

    user = User(
        sub=str(payload.get("sub", "")),
        email=email,
        name=str(payload.get("name", "")),
    )
    now = int(time.time())
    session_token = jwt.encode(
        {
            "sub": user.sub,
            "email": user.email,
            "name": user.name,
            "iat": now,
            "exp": now + SESSION_TTL_SECONDS,
        },
        JWT_SECRET,
        algorithm=ALGORITHM_HS,
    )
    return session_token, user


def _decode_session_token(token: str) -> User:
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM_HS])
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid session token: {e}") from e
    email = str(payload.get("email", "")).lower()
    if email not in ALLOWED_EMAILS:
        # Allowlist may have changed since the token was issued; reject stale sessions.
        raise HTTPException(status_code=403, detail="email no longer allowed")
    return User(sub=str(payload["sub"]), email=email, name=str(payload.get("name", "")))


def _token_from_request(authorization: str | None, auth_cookie: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    if auth_cookie:
        return auth_cookie
    raise HTTPException(status_code=401, detail="missing authentication")


def current_user(
    authorization: str | None = Header(default=None),
    auth_token: str | None = Cookie(default=None),
) -> User:
    """FastAPI dependency for REST endpoints."""
    return _decode_session_token(_token_from_request(authorization, auth_token))


def current_user_ws(ws: WebSocket) -> User:
    """Same check as current_user, but pulls headers/cookies off a raw WebSocket."""
    authorization = ws.headers.get("authorization")
    auth_cookie = ws.cookies.get(COOKIE_NAME)
    return _decode_session_token(_token_from_request(authorization, auth_cookie))
