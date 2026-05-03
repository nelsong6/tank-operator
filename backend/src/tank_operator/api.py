import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    User,
    _decode_session_token,
    current_user,
    current_user_ws,
    exchange_microsoft_token,
    mint_install_state,
    verify_install_state,
)
from .credentials_seed import CredentialsSeedError, harvest_and_save, harvest_codex_and_save
from .exec_proxy import bridge
from .profiles import ProfileStore
from .sessions import (
    DEFAULT_SESSION_MODE,
    SESSION_MODES,
    SESSIONS_NAMESPACE,
    PodNotReady,
    SessionInfo,
    SessionManager,
    SessionNotFound,
    SessionNotOwned,
)

sessions = SessionManager()
profiles = ProfileStore()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await profiles.startup()
    await sessions.startup()
    try:
        yield
    finally:
        await sessions.shutdown()
        await profiles.shutdown()


app = FastAPI(lifespan=lifespan)


class LoginBody(BaseModel):
    credential: str


class LoginResponse(BaseModel):
    token: str
    user: dict[str, str]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> dict[str, str]:
    """Public auth config consumed by the frontend to bootstrap MSAL."""
    return {
        "entra_client_id": os.environ.get("ENTRA_CLIENT_ID", ""),
        "entra_authority": "https://login.microsoftonline.com/common",
    }


@app.post("/api/auth/microsoft/login", response_model=LoginResponse)
async def microsoft_login(body: LoginBody, request: Request) -> JSONResponse:
    session_token, user = await exchange_microsoft_token(body.credential)
    # Ensure a profile row exists for the authenticated email. Cheap on
    # repeat logins (single-document read), creates the row only on first
    # login. Hooked here rather than in current_user so we don't add a
    # Cosmos round-trip to every request.
    profile = await profiles.get_or_create(user.email)
    secure = request.url.scheme == "https"
    response = JSONResponse(
        {
            "token": session_token,
            "user": {
                "sub": user.sub,
                "email": user.email,
                "name": user.name,
                "github_login": profile.github_login,
                "installation_id": profile.installation_id,
            },
        }
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/api/auth/me", response_model=dict)
async def me(user: User = Depends(current_user)) -> dict:
    """Identity + profile state for the signed-in user.

    `installation_id` is null until the user installs the GitHub App via the
    onboarding flow (#57 stage 2). The frontend uses its presence as the
    signal for whether to show the install wall.
    """
    profile = await profiles.get_or_create(user.email)
    return {
        "sub": user.sub,
        "email": user.email,
        "name": user.name,
        "github_login": profile.github_login,
        "installation_id": profile.installation_id,
    }


# ----------------------------------------------------------------------------
# GitHub App install flow (#57 stage 2)
#
# Onboarding wall in the SPA → /api/github/install/url 302s to GitHub's
# install page → user grants on GitHub → GitHub redirects to the App's
# Setup URL (configured in the GitHub UI to point at /api/github/install/
# callback) → callback persists installation_id on the profile and 302s
# back to /. Whole flow is browser-driven; no orchestrator-side outbound
# calls to GitHub.
# ----------------------------------------------------------------------------

GITHUB_APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "tank-operator")


@app.get("/api/github/install/url")
async def github_install_url(user: User = Depends(current_user)) -> RedirectResponse:
    """Redirect the caller to GitHub's install consent page.

    The state JWT binds the install flow to the caller's email so the
    callback can refuse a redirect that didn't originate from us. 10-min
    TTL — long enough for "click → grant → return" without leaving a stale
    token usable to retry later.
    """
    state = mint_install_state(user.email)
    target = f"https://github.com/apps/{GITHUB_APP_SLUG}/installations/new?state={state}"
    return RedirectResponse(url=target, status_code=302)


@app.get("/api/github/install/callback")
async def github_install_callback(
    request: Request,
    installation_id: int | None = None,
    setup_action: str | None = None,
    state: str | None = None,
    auth_token: str | None = Cookie(default=None),
) -> RedirectResponse:
    """GitHub's redirect target after install consent.

    Validates the state JWT we minted and the caller's session cookie agree
    on email — defense-in-depth against a phishing scenario where an
    attacker mints a state for their own email and tricks a victim into
    completing the install. Without the cookie check, the victim's
    installation_id would land under the attacker's profile.

    On any validation failure, redirect to /?install_error=<reason> so the
    SPA can render a banner instead of leaving the user on a backend 4xx.
    """

    def _err(reason: str) -> RedirectResponse:
        return RedirectResponse(url=f"/?install_error={reason}", status_code=302)

    if not state:
        return _err("missing_state")
    if not installation_id:
        # GitHub sends `setup_action=request` for org-controlled installs that
        # are pending admin approval; installation_id arrives later via the
        # webhook (out of scope for stage 2).
        return _err("pending_approval" if setup_action == "request" else "missing_installation_id")
    try:
        state_email = verify_install_state(state)
    except HTTPException:
        return _err("invalid_state")

    if not auth_token:
        return _err("session_expired")
    try:
        cookie_user = _decode_session_token(auth_token)
    except HTTPException:
        return _err("session_invalid")

    if cookie_user.email.lower() != state_email:
        return _err("email_mismatch")

    await profiles.update_installation(
        email=state_email, installation_id=installation_id, github_login=None
    )
    return RedirectResponse(url="/", status_code=302)


class CreateSessionBody(BaseModel):
    # Body is optional on the wire (POST with no JSON still works) so the
    # default-mode `+ new` button doesn't have to send anything.
    mode: str = DEFAULT_SESSION_MODE


@app.post("/api/sessions")
async def create_session(
    body: CreateSessionBody | None = None,
    user: User = Depends(current_user),
) -> SessionInfo:
    mode = body.mode if body else DEFAULT_SESSION_MODE
    if mode not in SESSION_MODES:
        raise HTTPException(status_code=400, detail=f"unknown mode: {mode}")
    return await sessions.create(owner=user.email, mode=mode)


@app.get("/api/sessions")
async def list_sessions(user: User = Depends(current_user)) -> list[SessionInfo]:
    return await sessions.list(owner=user.email)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, user: User = Depends(current_user)) -> dict[str, str]:
    try:
        await sessions.delete(owner=user.email, session_id=session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found")
    except SessionNotOwned:
        raise HTTPException(status_code=403, detail="session not owned by caller")
    return {"id": session_id, "status": "deleted"}


class PatchSessionBody(BaseModel):
    # Empty string / null clears the name; otherwise stored verbatim (trimmed
    # + length-capped server-side).
    name: str | None = None


@app.patch("/api/sessions/{session_id}")
async def patch_session(
    session_id: str,
    body: PatchSessionBody,
    user: User = Depends(current_user),
) -> SessionInfo:
    try:
        return await sessions.set_name(
            owner=user.email, session_id=session_id, name=body.name
        )
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found")
    except SessionNotOwned:
        raise HTTPException(status_code=403, detail="session not owned by caller")


@app.post("/api/sessions/{session_id}/save-credentials")
async def save_credentials(
    session_id: str, user: User = Depends(current_user)
) -> dict[str, str]:
    """Capture credentials from a *_config session and seed KV.

    Mode dispatch:
      - `config`        → ~/.claude/.credentials.json → KV `claude-code-credentials`
      - `codex_config`  → ~/.codex/auth.json          → KV `codex-credentials`

    Both paths only valid for their respective config modes — both as a
    UX guard (the button only shows on those tabs) and as defense-in-depth
    so a misconfigured caller can't dump credentials out of a regular
    session pod's mounted Secret.
    """
    try:
        session = await sessions.get_session(owner=user.email, session_id=session_id)
    except SessionNotOwned:
        raise HTTPException(status_code=403, detail="session not owned by caller")
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found")
    if session.mode not in ("config", "codex_config"):
        raise HTTPException(
            status_code=400,
            detail="save-credentials is only valid for config / codex_config sessions",
        )
    try:
        pod_name = await sessions.get_pod_name(owner=user.email, session_id=session_id)
    except PodNotReady:
        raise HTTPException(status_code=503, detail="pod not ready")
    try:
        if session.mode == "config":
            await harvest_and_save(namespace=SESSIONS_NAMESPACE, pod_name=pod_name)
        else:
            await harvest_codex_and_save(
                namespace=SESSIONS_NAMESPACE, pod_name=pod_name
            )
    except CredentialsSeedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": session_id, "status": "saved"}


@app.websocket("/api/sessions/{session_id}/exec")
async def session_exec(ws: WebSocket, session_id: str) -> None:
    # Accept up front so we can send a close frame the browser can read
    # (`reason` is dropped by Starlette/most browsers when close is called
    # before accept — the tab just sees code 1006, no detail).
    await ws.accept()
    try:
        user = current_user_ws(ws)
    except HTTPException as e:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason=e.detail)
        return

    try:
        pod_name = await sessions.get_pod_name(owner=user.email, session_id=session_id)
    except SessionNotOwned:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="not owner")
        return
    except SessionNotFound:
        await ws.close(code=status.WS_1011_INTERNAL_ERROR, reason="session not found")
        return
    except PodNotReady:
        await ws.close(code=status.WS_1011_INTERNAL_ERROR, reason="pod not ready")
        return

    async with sessions.track_ws(session_id):
        try:
            await bridge(ws, namespace=SESSIONS_NAMESPACE, pod_name=pod_name)
        except WebSocketDisconnect:
            pass


_static_env = os.environ.get("TANK_OPERATOR_STATIC_DIR")
_static = Path(_static_env) if _static_env else Path(__file__).resolve().parent / "static"
if _static.exists():
    app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_static / "index.html")

    @app.get("/_styleguide")
    async def styleguide() -> FileResponse:
        # SPA-served. The Vite bundle's main.tsx routes to StyleguideView
        # when window.location.pathname matches; we just need to serve
        # the same index.html so the bundle loads. Glimmung's UI pilot
        # contract — see frontend/src/StyleguideView.tsx and the
        # docs/styleguide-contract.md in the glimmung repo.
        return FileResponse(_static / "index.html")
