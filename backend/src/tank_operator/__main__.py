"""Entrypoint for the tank-operator orchestrator.

Listens on two ports when the OAuth gateway TLS material is present:
  - PORT (default 8000): plain HTTP, fronted by Envoy Gateway with letsencrypt
    for the public API at tank.romaine.life.
  - OAUTH_GATEWAY_PORT (default 8443): HTTPS with a self-signed leaf for
    `platform.claude.com`. Reached only by session pods inside the cluster
    via /etc/hosts override + NODE_EXTRA_CA_CERTS pointing at our CA.

If the cert files aren't mounted (e.g. local dev), only the HTTP port is
served, so `python -m tank_operator` still works without cert-manager.
"""
import asyncio
import os
from pathlib import Path

import uvicorn


def _http_config() -> uvicorn.Config:
    return uvicorn.Config(
        "tank_operator.api:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )


def _tls_config() -> uvicorn.Config | None:
    cert = os.environ.get("OAUTH_GATEWAY_TLS_CERT", "/etc/oauth-gateway-tls/tls.crt")
    key = os.environ.get("OAUTH_GATEWAY_TLS_KEY", "/etc/oauth-gateway-tls/tls.key")
    if not (Path(cert).exists() and Path(key).exists()):
        return None
    return uvicorn.Config(
        "tank_operator.api:app",
        host="0.0.0.0",
        port=int(os.environ.get("OAUTH_GATEWAY_PORT", "8443")),
        ssl_certfile=cert,
        ssl_keyfile=key,
        log_level="info",
    )


async def _serve_all() -> None:
    configs = [_http_config()]
    tls = _tls_config()
    if tls is not None:
        configs.append(tls)
    servers = [uvicorn.Server(c) for c in configs]
    await asyncio.gather(*(s.serve() for s in servers))


def main() -> None:
    asyncio.run(_serve_all())


if __name__ == "__main__":
    main()
