import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .exec_proxy import bridge
from .sessions import (
    SESSIONS_NAMESPACE,
    PodNotReady,
    SessionInfo,
    SessionManager,
    SessionNotFound,
    SessionNotOwned,
)

sessions = SessionManager()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await sessions.startup()
    try:
        yield
    finally:
        await sessions.shutdown()


app = FastAPI(lifespan=lifespan)


def _user(email: str | None) -> str:
    if not email:
        raise HTTPException(status_code=401, detail="missing X-Auth-Request-Email")
    return email


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/sessions")
async def create_session(
    x_auth_request_email: str | None = Header(default=None),
) -> SessionInfo:
    return await sessions.create(owner=_user(x_auth_request_email))


@app.get("/api/sessions")
async def list_sessions(
    x_auth_request_email: str | None = Header(default=None),
) -> list[SessionInfo]:
    return await sessions.list(owner=_user(x_auth_request_email))


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str,
    x_auth_request_email: str | None = Header(default=None),
) -> dict[str, str]:
    try:
        await sessions.delete(owner=_user(x_auth_request_email), session_id=session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="session not found")
    except SessionNotOwned:
        raise HTTPException(status_code=403, detail="session not owned by caller")
    return {"id": session_id, "status": "deleted"}


@app.websocket("/api/sessions/{session_id}/exec")
async def session_exec(ws: WebSocket, session_id: str) -> None:
    email = ws.headers.get("x-auth-request-email")
    if not email:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="missing X-Auth-Request-Email")
        return

    try:
        pod_name = await sessions.get_pod_name(owner=email, session_id=session_id)
    except SessionNotOwned:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="not owner")
        return
    except SessionNotFound:
        await ws.close(code=status.WS_1011_INTERNAL_ERROR, reason="session not found")
        return
    except PodNotReady:
        await ws.close(code=status.WS_1011_INTERNAL_ERROR, reason="pod not ready")
        return

    await ws.accept()
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
