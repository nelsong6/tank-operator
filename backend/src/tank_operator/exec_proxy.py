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

# Pre-seed claude's first-run state so a fresh pod boots straight to the chat
# prompt — no theme picker, no "trust this folder?", no "approve this API key?",
# no MCP marketplace prompt, no "approve project MCP servers?", and no
# per-tool prompts (Bash, MCP, Edit, ...). The session pod is a sandboxed,
# ephemeral, single-tenant execution environment — there's nothing the user
# would gain from approving each call individually, so we run claude in
# bypassPermissions mode and pre-accept the bypass-acknowledgement dialog.
# State lives in:
#   ~/.claude/settings.json       — theme + bypassPermissions defaultMode +
#                                   skipDangerousModePermissionPrompt
#   ~/.claude.json                — onboarding flag + API-key trust list
#                                   (claude keys off the last 20 chars; we
#                                   include 22 too in case that flips back) +
#                                   per-project trust for /workspace +
#                                   official-marketplace auto-install flags +
#                                   pre-approved set of project-level MCP
#                                   servers (read from /workspace/.mcp.json so
#                                   it stays correct as the image evolves; the
#                                   file itself is baked into claude-container
#                                   from claude-container/mcp.json in this
#                                   repo, along with the env-var contract its
#                                   servers expect)
#   ~/.claude/.credentials.json   — only in subscription mode: a credentials
#                                   blob fetched at boot from the orchestrator's
#                                   in-cluster OAuth gateway (which impersonates
#                                   platform.claude.com via /etc/hosts +
#                                   NODE_EXTRA_CA_CERTS). The blob has a fresh
#                                   access token + a placeholder refresh token —
#                                   the real refresh token never enters the
#                                   pod. When claude later refreshes, it hits
#                                   the same gateway and gets a new access
#                                   token; the gateway is the only thing that
#                                   ever touches Anthropic's OAuth endpoint.
# Then exec claude. If claude exits we drop into bash so the WS stays useful.
#
# `customApiKeyResponses` only matters in api_key mode; in subscription mode
# claude reads from .credentials.json and never prompts about an API key.
# Unsetting ANTHROPIC_API_KEY in subscription mode is important — if both are
# present, claude prefers the API key and bills against it.
_BOOTSTRAP_SH = r"""
# Read the projected SA token and export it as the Authorization bearer
# for both HTTP MCP servers. claude-container's image-level entrypoint.sh
# does this too, but kubectl exec starts a fresh shell that doesn't
# inherit env from PID 1 — so we have to redo it here for the in-pod
# claude process to pick it up.
TOKEN_PATH=/var/run/secrets/kubernetes.io/serviceaccount/token
if [ -r "$TOKEN_PATH" ]; then
  TOKEN="$(cat $TOKEN_PATH)"
  export MCP_AZURE_BEARER="$TOKEN"
  export MCP_GITHUB_BEARER="$TOKEN"
fi
mkdir -p $HOME/.claude
cat > $HOME/.claude/settings.json <<'EOF'
{"theme":"dark","permissions":{"defaultMode":"bypassPermissions"},"skipDangerousModePermissionPrompt":true}
EOF
mcp_enabled='[]'
if [ -f /workspace/.mcp.json ]; then
  mcp_enabled="$(jq -c '.mcpServers | keys' /workspace/.mcp.json)"
fi
case "${TANK_SESSION_MODE:-api_key}" in
  subscription)
    # Fetch a fresh credentials.json from the in-cluster OAuth gateway. The
    # gateway returns the original blob shape (preserved from KV) but with a
    # fresh access token from its single-flight cache and a placeholder
    # refresh token, so the pod never gets the real refresh token. /etc/hosts
    # routes platform.claude.com to the gateway Service; NODE_EXTRA_CA_CERTS
    # makes the cluster's self-signed CA trusted so curl + claude both
    # accept the gateway's cert.
    creds_path=$HOME/.claude/.credentials.json
    if curl -sS --fail --max-time 15 \
         --cacert /etc/oauth-gateway-ca/ca.crt \
         "https://platform.claude.com/internal/credentials-bootstrap" \
         -o "$creds_path"; then
      chmod 600 "$creds_path"
    else
      echo "tank-operator: failed to fetch credentials from OAuth gateway." >&2
      echo "Check that claude-oauth-gateway service is healthy and the CA" >&2
      echo "ConfigMap is reflected into this namespace; the session will" >&2
      echo "fall through to claude /login, which won't work without a browser." >&2
    fi
    unset ANTHROPIC_API_KEY
    api_key_block=''
    ;;
  *)
    last20="${ANTHROPIC_API_KEY: -20}"
    last22="${ANTHROPIC_API_KEY: -22}"
    api_key_block="\"customApiKeyResponses\": {\"approved\": [\"${last20}\", \"${last22}\"], \"rejected\": []},"
    ;;
esac
cat > $HOME/.claude.json <<EOF
{
  "hasCompletedOnboarding": true,
  ${api_key_block}
  "officialMarketplaceAutoInstallAttempted": true,
  "officialMarketplaceAutoInstalled": true,
  "projects": {
    "/workspace": {
      "allowedTools": [],
      "mcpContextUris": [],
      "mcpServers": {},
      "enabledMcpjsonServers": ${mcp_enabled},
      "disabledMcpjsonServers": [],
      "hasTrustDialogAccepted": true,
      "projectOnboardingSeenCount": 1,
      "hasClaudeMdExternalIncludesApproved": false,
      "hasClaudeMdExternalIncludesWarningShown": false,
      "lastGracefulShutdown": false
    }
  }
}
EOF
claude
exec bash
"""
EXEC_COMMAND = ["bash", "-lc", _BOOTSTRAP_SH]

STDIN_CHANNEL = 0
STDOUT_CHANNEL = 1
STDERR_CHANNEL = 2
ERROR_CHANNEL = 3
RESIZE_CHANNEL = 4


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
