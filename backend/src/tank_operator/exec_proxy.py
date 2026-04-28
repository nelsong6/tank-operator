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
#   ~/.claude/.credentials.json   — only in subscription mode: a static
#                                   placeholder blob. The real token is
#                                   never written to the pod. The
#                                   in-cluster api-proxy (api.anthropic.com
#                                   redirected to a Service via hostAlias,
#                                   trusted via NODE_EXTRA_CA_CERTS) strips
#                                   whatever Authorization claude sends and
#                                   injects the current real Bearer on the
#                                   way upstream. claude believes it's
#                                   talking to api.anthropic.com directly.
# claude runs inside a named tmux session ("tank") so that when the
# browser WS drops and reconnects, the new kubectl-exec re-attaches to
# the same session — preserving the in-progress conversation, scrollback,
# and PTY state. Without this, every reconnect spawned a fresh `claude`.
# If claude exits inside the session we drop into bash so the WS stays
# useful (and the tmux window doesn't disappear).
#
# `customApiKeyResponses` only matters in api_key mode; in subscription mode
# claude reads from .credentials.json and never prompts about an API key.
# Unsetting ANTHROPIC_API_KEY in subscription mode is important — if both are
# present, claude prefers the API key and bills against it.
_BOOTSTRAP_SH = r"""
# Reconnect fast-path: if the tmux session already exists this is a
# reattach, not a fresh boot. Skip settings/credentials setup (already
# done on first connect; rewriting is idempotent but wasteful, and in
# subscription mode would re-hit the OAuth gateway every reconnect).
if tmux has-session -t tank 2>/dev/null; then
  exec tmux attach-session -t tank
fi
# Config-mode: short-circuit the regular session bootstrap. The user is
# here to do `claude /login` once so we can capture credentials.json and
# write it to KV. No MCP wiring, no onboarding bypass, no credentials
# pre-seed — claude needs to see a clean state to walk through OAuth.
# The orchestrator's POST /api/sessions/{id}/save-credentials reads the
# resulting ~/.claude/.credentials.json out of this pod via exec.
if [ "${TANK_SESSION_MODE}" = "config" ]; then
  mkdir -p $HOME/.claude
  cat > $HOME/.claude/settings.json <<'EOF'
{"theme":"dark"}
EOF
  cat > $HOME/.claude.json <<'EOF'
{"hasCompletedOnboarding": true}
EOF
  exec claude /login
fi
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
    # Static placeholder credentials. The api-proxy in front of
    # api.anthropic.com strips this Authorization on every request and
    # injects the real token, so claude never needs valid creds locally.
    # expiresAt is set to year 2286 so claude never decides to refresh
    # on its own; the placeholder refreshToken would 400 immediately at
    # platform.claude.com if it ever did.
    creds_path=$HOME/.claude/.credentials.json
    cat > "$creds_path" <<'EOF'
{
  "claudeAiOauth": {
    "accessToken": "managed-by-tank-operator",
    "refreshToken": "managed-by-tank-operator",
    "expiresAt": 9999999999000,
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "max",
    "rateLimitTier": "max"
  }
}
EOF
    chmod 600 "$creds_path"
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
exec tmux new-session -s tank 'claude; exec bash'
"""
EXEC_COMMAND = ["bash", "-lc", _BOOTSTRAP_SH]

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
