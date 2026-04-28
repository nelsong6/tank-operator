"""HTTP entrypoint — streamable-http transport, no incoming auth.

Auth is handled by kube-rbac-proxy in front of this process: clients present
a K8s SA token, the proxy validates it via TokenReview + SubjectAccessReview
(see ../k8s-mcp-k8s/templates/proxy-config.yaml), and only authorized
requests reach this server. Binding loopback so direct pod-IP:8080 access
bypasses nothing — only the proxy can talk to us.

Outbound auth (kubectl + helm) uses the in-cluster ServiceAccount token at
/var/run/secrets/kubernetes.io/serviceaccount/token. No kubeconfig needed —
the binaries pick that up automatically.
"""

import logging
import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from .tools import register_tools


def build_app() -> Starlette:
    # Same DNS-rebinding-protection workaround as mcp-github: the streamable_http
    # transport ships a middleware that 421s any Host header not in
    # `allowed_hosts`. The default whitelist only covers localhost, so
    # in-cluster requests to mcp-k8s.mcp-k8s.svc would be rejected. Disable
    # here — kube-rbac-proxy in front of us already gates auth via K8s SA
    # tokens, so DNS rebinding can't reach an unauthorized caller anyway.
    # streamable_http_path="/" avoids Starlette's trailing-slash redirect
    # (was 307 → 421 loop in mcp-github).
    mcp = FastMCP(
        "k8s-mcp",
        stateless_http=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    register_tools(mcp)

    async def healthz(_: Request) -> Response:
        return Response("ok", media_type="text/plain")

    # Starlette's Mount doesn't forward lifespan events to the inner app, so
    # FastMCP's session_manager.run() — which sets up the anyio task group
    # the streamable-http handler depends on — never fires when we mount it.
    # Wire the run() context into the outer app's lifespan ourselves; without
    # this every request 500s with "Task group is not initialized".
    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
