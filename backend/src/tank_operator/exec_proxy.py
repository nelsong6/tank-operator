"""Bridges a FastAPI WebSocket to a K8s pods/exec WebSocket.

Frontend protocol (kept deliberately small):
- Text frames are stdin (utf-8 encoded keystrokes from xterm.js).
- A text frame parsing as JSON `{"resize": [cols, rows]}` is interpreted as
  a terminal-resize control message instead of stdin.
- Server emits raw stdout/stderr bytes from the pod as text frames.

K8s pods/exec uses channelled binary frames per `v4.channel.k8s.io`:
channel 0=stdin, 1=stdout, 2=stderr, 3=error, 4=resize. The
kubernetes_asyncio stream client wraps the channelling.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect
from kubernetes_asyncio import client
from kubernetes_asyncio.stream import WsApiClient

log = logging.getLogger(__name__)

EXEC_COMMAND = ["/bin/bash"]


async def bridge(browser: WebSocket, namespace: str, pod_name: str) -> None:
    ws_client = WsApiClient()
    core = client.CoreV1Api(api_client=ws_client)

    k8s_ws = await core.connect_get_namespaced_pod_exec(
        name=pod_name,
        namespace=namespace,
        command=EXEC_COMMAND,
        stdin=True,
        stdout=True,
        stderr=True,
        tty=True,
        _preload_content=False,
    )

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
                    handled = False
                    if text and text[0] == "{":
                        try:
                            ctrl = json.loads(text)
                        except ValueError:
                            ctrl = None
                        if isinstance(ctrl, dict) and "resize" in ctrl:
                            cols, rows = ctrl["resize"]
                            await k8s_ws.write_channel(
                                4,
                                json.dumps({"Width": int(cols), "Height": int(rows)}),
                            )
                            handled = True
                    if not handled:
                        await k8s_ws.write_stdin(text)
                else:
                    data = msg.get("bytes")
                    if data:
                        await k8s_ws.write_stdin(data.decode("utf-8", errors="replace"))
        except WebSocketDisconnect:
            return
        except Exception:
            log.exception("browser → pod loop crashed")

    async def pod_to_browser() -> None:
        try:
            while True:
                if not getattr(k8s_ws, "open", True):
                    return
                stdout = await k8s_ws.read_stdout(timeout=1)
                if stdout:
                    await browser.send_text(stdout)
                stderr = await k8s_ws.read_stderr(timeout=0.01)
                if stderr:
                    await browser.send_text(stderr)
        except Exception:
            log.exception("pod → browser loop crashed")

    try:
        await asyncio.gather(browser_to_pod(), pod_to_browser())
    finally:
        try:
            await k8s_ws.close()
        except Exception:
            pass
        await ws_client.close()
