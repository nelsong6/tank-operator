"""Bridges a FastAPI WebSocket to a K8s pods/exec WebSocket.

The k8s pods/exec endpoint speaks the `v4.channel.k8s.io` protocol: binary
frames whose first byte is the channel (0=stdin, 1=stdout, 2=stderr,
3=error, 4=resize) followed by the payload. kubernetes_asyncio 35 exposes
the raw aiohttp WebSocket — we do the channel framing ourselves.

Frontend protocol (kept deliberately small):
- Text frames are stdin (utf-8 keystrokes from xterm.js).
- A text frame parsing as JSON `{"resize": [cols, rows]}` is a terminal
  resize control message instead of stdin.
- Server emits raw stdout/stderr bytes from the pod as text frames.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
from fastapi import WebSocket, WebSocketDisconnect
from kubernetes_asyncio import client
from kubernetes_asyncio.stream import WsApiClient

log = logging.getLogger(__name__)

# Bootstrap is baked into the session image at /opt/tank/bootstrap.sh
# (see claude-container/tank-bootstrap.sh). Inlining it here is tempting
# but the kube-apiserver URL-encodes every byte of the exec command into
# ?command=... and rejects oversized request lines with HTTP 400; the
# script grew past that limit and broke reconnects.
EXEC_COMMAND = ["bash", "-l", "/opt/tank/bootstrap.sh"]

STDIN_CHANNEL = 0
STDOUT_CHANNEL = 1
STDERR_CHANNEL = 2
ERROR_CHANNEL = 3
RESIZE_CHANNEL = 4


async def exec_capture(namespace: str, pod_name: str, command: list[str]) -> bytes:
    """Run a one-shot command in `pod_name` and return its stdout as bytes.

    Used for short, read-only operations (e.g. `cat /some/file`) where the
    caller needs the bytes back as a single buffer. For interactive long-
    lived streams (TTY shells), use `bridge` instead.

    Raises RuntimeError if the K8s exec error channel reports a non-Success
    status (typical when the command exits non-zero, e.g. cat on a missing
    file). stderr is logged at WARNING but not surfaced to the caller —
    callers that care should check command output instead.
    """
    ws_client = WsApiClient()
    core = client.CoreV1Api(api_client=ws_client)
    try:
        cm = await core.connect_get_namespaced_pod_exec(
            name=pod_name,
            namespace=namespace,
            command=command,
            stdin=False,
            stdout=True,
            stderr=True,
            tty=False,
            _preload_content=False,
        )
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        error_status: dict[str, str] | None = None
        async with cm as k8s_ws:
            async for wsmsg in k8s_ws:
                if wsmsg.type == aiohttp.WSMsgType.BINARY:
                    if not wsmsg.data:
                        continue
                    channel = wsmsg.data[0]
                    payload = wsmsg.data[1:]
                    if channel == STDOUT_CHANNEL:
                        stdout_chunks.append(payload)
                    elif channel == STDERR_CHANNEL:
                        stderr_chunks.append(payload)
                    elif channel == ERROR_CHANNEL:
                        # K8s sends a v1.Status JSON here at end-of-stream;
                        # {"status":"Success"} on exit-0, otherwise a
                        # Failure with details (including non-zero exit
                        # code in `details.causes[].message`).
                        try:
                            error_status = json.loads(payload)
                        except ValueError:
                            error_status = {"status": "Failure", "message": payload.decode(errors="replace")}
                elif wsmsg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
    finally:
        await ws_client.close()

    if stderr_chunks:
        log.warning(
            "exec %s stderr: %s",
            command,
            b"".join(stderr_chunks).decode(errors="replace")[:500],
        )
    if error_status is not None and error_status.get("status") != "Success":
        raise RuntimeError(f"exec {command} failed: {error_status}")
    return b"".join(stdout_chunks)


async def bridge(browser: WebSocket, namespace: str, pod_name: str) -> None:
    ws_client = WsApiClient()
    core = client.CoreV1Api(api_client=ws_client)

    # _preload_content=False makes WsApiClient return the aiohttp
    # ws_connect() context manager directly; await + async-with to get the
    # ClientWebSocketResponse.
    cm = await core.connect_get_namespaced_pod_exec(
        name=pod_name,
        namespace=namespace,
        command=EXEC_COMMAND,
        stdin=True,
        stdout=True,
        stderr=True,
        tty=True,
        _preload_content=False,
    )

    try:
        async with cm as k8s_ws:
            await _pump(browser, k8s_ws)
    finally:
        await ws_client.close()


async def _pump(browser: WebSocket, k8s_ws: aiohttp.ClientWebSocketResponse) -> None:
    async def send_channel(channel: int, payload: bytes | str) -> None:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        await k8s_ws.send_bytes(bytes([channel]) + data)

    async def browser_to_pod() -> None:
        try:
            while True:
                msg = await browser.receive()
                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
                    return
                if msg_type != "websocket.receive":
                    continue

                text = msg.get("text")
                if text is not None:
                    # Control frames look like JSON; everything else is raw
                    # stdin from xterm.js. Recognized: {"resize":[c,r]} for
                    # PTY size changes, {"ping":...} as a no-op heartbeat the
                    # browser sends every ~30s so Envoy's idle stream timeout
                    # (default 5min) doesn't cut a quiet WS — which would also
                    # let the orchestrator's idle reaper delete the pod.
                    if text and text[0] == "{":
                        try:
                            ctrl = json.loads(text)
                        except ValueError:
                            ctrl = None
                        if isinstance(ctrl, dict):
                            if "resize" in ctrl:
                                cols, rows = ctrl["resize"]
                                await send_channel(
                                    RESIZE_CHANNEL,
                                    json.dumps({"Width": int(cols), "Height": int(rows)}),
                                )
                                continue
                            if "ping" in ctrl:
                                continue
                    await send_channel(STDIN_CHANNEL, text)
                else:
                    data = msg.get("bytes")
                    if data:
                        await send_channel(STDIN_CHANNEL, data)
        except WebSocketDisconnect:
            return
        except Exception:
            log.exception("browser → pod loop crashed")

    async def pod_to_browser() -> None:
        try:
            async for wsmsg in k8s_ws:
                if wsmsg.type == aiohttp.WSMsgType.BINARY:
                    if not wsmsg.data:
                        continue
                    channel = wsmsg.data[0]
                    payload = wsmsg.data[1:]
                    if not payload:
                        continue
                    if channel in (STDOUT_CHANNEL, STDERR_CHANNEL):
                        await browser.send_text(payload.decode("utf-8", errors="replace"))
                    elif channel == ERROR_CHANNEL:
                        log.warning("k8s exec error frame: %s", payload)
                elif wsmsg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    return
        except Exception:
            log.exception("pod → browser loop crashed")

    await asyncio.gather(browser_to_pod(), pod_to_browser())
