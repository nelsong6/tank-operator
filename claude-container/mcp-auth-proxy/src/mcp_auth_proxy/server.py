"""Localhost reverse proxy that injects fresh SA-token bearer auth.

Sidecar to the claude-container in each session pod. Listens on per-MCP
localhost ports; .mcp.json points claude at these instead of at the
in-cluster MCP Services directly. Every request reads the projected
ServiceAccount token from disk and forwards upstream with
Authorization: Bearer <fresh>.

The bug this exists to fix: kubelet rotates the SA token file in-place
(eager renewal at ~50 min, well inside the default 1h TTL), but env
vars set from that file at pod start go stale. The previous wiring
exported MCP_*_BEARER once in entrypoint.sh and again in the bootstrap
shell, then substituted them into .mcp.json's Authorization headers at
harness startup — so any MCP call past the 1h boundary 401'd until the
session was recreated. This proxy reads the file fresh on every
request, so token rotation is invisible to claude.

Same shape as api-proxy (the in-cluster header-injecting proxy for
api.anthropic.com), just localized to the pod because the SA token is
per-pod identity. Hardcoded LISTENERS map mirrors the entries in
claude-container/mcp.json — keep them in sync; both sides describe the
same set of MCPs from opposite ends.
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

log = logging.getLogger(__name__)

TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")

# (port, upstream URL). Mirrors claude-container/mcp.json. Adding an
# MCP means: append here, append a port mapping in mcp.json, ship.
LISTENERS: list[tuple[int, str]] = [
    (9991, "http://mcp-azure.mcp-azure.svc:80"),
    (9992, "http://mcp-github.mcp-github.svc:80"),
    (9993, "http://mcp-k8s.mcp-k8s.svc:80"),
    (9994, "http://mcp-argocd.mcp-argocd.svc:80"),
    (9995, "http://mcp-glimmung.mcp-glimmung.svc:80"),
]

# Headers we strip from the inbound request before forwarding. Host is
# rebuilt by aiohttp for the upstream; Authorization gets replaced with
# the fresh SA token; hop-by-hop and content-length are recomputed.
_STRIP_REQUEST_HEADERS = frozenset(
    {"host", "authorization", "content-length", "connection", "transfer-encoding"}
)
# Same idea on the way back — let aiohttp set framing headers on the
# response we stream to the client.
_STRIP_RESPONSE_HEADERS = frozenset(
    {"transfer-encoding", "content-encoding", "connection", "content-length"}
)


def _read_token() -> str:
    return TOKEN_PATH.read_text().strip()


def _make_handler(upstream: str, http: ClientSession):
    upstream = upstream.rstrip("/")

    async def handler(request: web.Request) -> web.StreamResponse:
        try:
            token = _read_token()
        except OSError:
            log.exception("could not read SA token at %s", TOKEN_PATH)
            return web.Response(status=503, text="SA token unavailable")

        forwarded_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS
        }
        forwarded_headers["Authorization"] = f"Bearer {token}"

        body = await request.read()
        url = upstream + request.path_qs
        try:
            async with http.request(
                request.method,
                url,
                headers=forwarded_headers,
                data=body,
                allow_redirects=False,
            ) as upstream_resp:
                response = web.StreamResponse(
                    status=upstream_resp.status,
                    headers={
                        k: v
                        for k, v in upstream_resp.headers.items()
                        if k.lower() not in _STRIP_RESPONSE_HEADERS
                    },
                )
                await response.prepare(request)
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except Exception:
            log.exception("upstream request to %s failed", url)
            return web.Response(status=502, text="upstream request failed")

    return handler


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Long total timeout — MCP tool calls can be minutes. Connect timeout
    # short so a dead upstream Service surfaces fast instead of hanging
    # the user-visible MCP call.
    http = ClientSession(timeout=ClientTimeout(total=600, sock_connect=5))
    runners: list[web.AppRunner] = []
    try:
        for port, upstream in LISTENERS:
            app = web.Application()
            app.router.add_route("*", "/{tail:.*}", _make_handler(upstream, http))
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            log.info("listening on 127.0.0.1:%d → %s", port, upstream)
            runners.append(runner)
        # Park forever; container lifecycle owns us.
        import asyncio

        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()
        await http.close()
