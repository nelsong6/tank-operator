import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import COOKIE_NAME, SESSION_TTL_SECONDS, User, current_user, current_user_ws, exchange_microsoft_token
from .credentials_seed import CredentialsSeedError, harvest_and_save
from .exec_proxy import bridge
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await sessions.startup()
    try:
        yield
    finally:
        await sessions.shutdown()


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
    secure = request.url.scheme == "https"
    response = JSONResponse(
        {"token": session_token, "user": {"sub": user.sub, "email": user.email, "name": user.name}}
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
async def me(user: User = Depends(current_user)) -> dict[str, str]:
    return {"sub": user.sub, "email": user.email, "name": user.name}


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
    """Capture ~/.claude/.credentials.json from a config-mode session and seed KV.

    Only valid for sessions in `config` mode — both as a UX guard
    (the button only shows on those tabs) and as a defense-in-depth check
    so a misconfigured caller can't dump credentials out of a regular
    session pod's mounted Secret. After write, ESO mirrors KV → the
    api-proxy's mounted Secret within ~1m and the proxy's ext_proc
    sidecar takes over rotation from the next upstream 401.
    """
    try:
        session = await sessions.get_session(owner=user.email, session_id=session_id)
    except SessionNotOwned:
        raise HTTPException(status_code=403, detail="session not owned by caller")
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found")
    if session.mode != "config":
        raise HTTPException(
            status_code=400,
            detail="save-credentials is only valid for config-mode sessions",
        )
    try:
        pod_name = await sessions.get_pod_name(owner=user.email, session_id=session_id)
    except PodNotReady:
        raise HTTPException(status_code=503, detail="pod not ready")
    try:
        await harvest_and_save(namespace=SESSIONS_NAMESPACE, pod_name=pod_name)
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
