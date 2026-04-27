import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import COOKIE_NAME, SESSION_TTL_SECONDS, User, current_user, current_user_ws, exchange_microsoft_token
from .exec_proxy import bridge
from .oauth_gateway import OAuthGateway, handle_bootstrap_blob, handle_oauth_token
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
    # SessionManager owns the K8s ApiClient, so reuse it for the OAuth gateway
    # rather than opening a second one.
    app.state.oauth_gateway = OAuthGateway(sessions._api)  # type: ignore[arg-type]
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


@app.post("/v1/oauth/token")
async def oauth_token(request: Request) -> dict[str, object]:
    """OAuth gateway: impersonates platform.claude.com's token endpoint.

    Reachable two ways:
      - On the public listener: returns 404 because the host check fails
        (Envoy rewrites Host to tank.romaine.life). Inert.
      - On the in-cluster TLS listener: session pods reach this via a
        /etc/hosts override mapping platform.claude.com to the orchestrator
        service IP, so the Host header arrives as platform.claude.com and
        the gateway answers.
    See oauth_gateway.py for the rationale and single-flight caching design.
    """
    return await handle_oauth_token(request)


@app.get("/internal/credentials-bootstrap")
async def credentials_bootstrap(request: Request) -> dict[str, object]:
    """Returns a complete credentials.json for a session pod to write to disk.

    Called once by the session container's bootstrap script. Same hostname
    gate as /v1/oauth/token — only reachable via the in-cluster TLS
    listener. The blob has the full original credentials.json shape (so we
    don't have to hardcode the schema) but with a fresh access token and a
    placeholder refresh token, so the pod never touches the real refresh
    token.
    """
    return await handle_bootstrap_blob(request)


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
