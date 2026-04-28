"""Envoy ext_proc service that injects Anthropic OAuth into outbound requests.

The Envoy listener in front of api.anthropic.com calls this service via the
``ExternalProcessor.Process`` bidirectional stream once per HTTP transaction.
We act on two messages per stream:

  - ``request_headers``: strip whatever Authorization the session pod sent
    (always the placeholder ``managed-by-tank-operator`` from the bootstrap),
    inject the current real access token from cache.
  - ``response_headers``: peek ``:status``. On 401 kick off a refresh in the
    background — Envoy's per-route retry policy resends the request, and by
    the time the retry's request_headers arrives at us the lock either holds
    a fresh token or makes the retry wait until one is in cache.

State:
  - ``_cached_access`` / ``_cached_refresh``: in-memory copy of the latest
    tokens. Initialized lazily from the mounted credentials.json the first
    time we need them, refreshed in place by ``_refresh()``.
  - ``_lock``: serializes refreshes so a 401 storm fans into exactly one
    upstream call to platform.claude.com.

KV write failure is non-fatal: see the comment on ``_persist_to_kv``.

Anthropic-side gating gotcha (burned us during rollout verification):
api.anthropic.com's /v1/messages endpoint refuses subscription OAuth tokens
unless TWO request headers are set, and the error messages are misleading:

  - Without ``anthropic-beta: oauth-2025-04-20`` →
    401 "OAuth authentication is currently not supported" (it IS supported,
    just gated behind the beta opt-in).
  - Without ``anthropic-dangerous-direct-browser-access: true`` →
    401 "Invalid authentication credentials" even on a freshly-minted token.

claude-code itself sends both headers natively, so traffic from session
pods works without this proxy adding them. We do NOT inject these here:
doing so would mask a future claude-code header change rather than
surfacing it. If you're synthetically curling api.anthropic.com to test
this proxy, set both headers; if a real claude-code session 401s when
synthetic curls succeed, the gate has moved and claude-code needs a bump.
The beta string above is hardcoded in claude-code's bundled JS as of
April 2026 — Anthropic rotating it would silently break direct callers
but not claude-code, which ships the matching value.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

import grpc
import httpx
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

from envoy.service.ext_proc.v3 import external_processor_pb2 as ext_proc_pb2
from envoy.service.ext_proc.v3 import external_processor_pb2_grpc as ext_proc_grpc
from envoy.config.core.v3 import base_pb2
from envoy.type.v3 import http_status_pb2

log = logging.getLogger(__name__)

# Hardcoded into Claude Code's bundled JS. Two distinct client_ids ship in
# the bundle: 22422756-... is paired with the legacy console.anthropic.com
# endpoint, 9d1c250a-... with platform.claude.com (our token URL). Tied
# here by the MANUAL_REDIRECT_URL/TOKEN_URL pairing in cli.js. The token
# URL is intentionally NOT routed through the proxy itself — the proxy
# fronts api.anthropic.com, not platform.claude.com.
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"

CREDENTIALS_FILE = os.environ.get(
    "CLAUDE_CREDENTIALS_FILE", "/etc/claude-credentials/credentials.json"
)


def _walk_for(blob: Any, names: tuple[str, ...]) -> str | None:
    if not isinstance(blob, dict):
        return None
    for k, v in blob.items():
        if k in names and isinstance(v, str):
            return v
        if isinstance(v, dict):
            found = _walk_for(v, names)
            if found:
                return found
    return None


def _patch_blob(blob: dict[str, Any], new_access: str, new_refresh: str, expires_in: int) -> dict[str, Any]:
    import time

    expires_at_ms = int((time.time() + expires_in) * 1000)
    out = json.loads(json.dumps(blob))

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for key in list(node.keys()):
            if key in ("accessToken", "access_token"):
                node[key] = new_access
            elif key in ("refreshToken", "refresh_token"):
                node[key] = new_refresh
            elif key in ("expiresAt", "expires_at"):
                node[key] = expires_at_ms
            elif isinstance(node[key], dict):
                walk(node[key])

    walk(out)
    return out


class AuthInjector(ext_proc_grpc.ExternalProcessorServicer):
    def __init__(self) -> None:
        self._cached_access: str | None = None
        self._cached_refresh: str | None = None
        self._cached_blob: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        # Set when an upstream 401 is observed for the current cached
        # token. The next request_headers callback awaits the lock (forcing
        # a refresh to complete) before injecting; once refresh updates
        # _cached_access we clear the flag so subsequent requests don't
        # block. Without this, envoy's retry would re-inject the same
        # invalid token and the retry would 401 too.
        self._access_invalidated = False
        self._kv_url = os.environ.get("AZURE_KEYVAULT_URL", "")
        self._kv_secret_name = os.environ.get(
            "CLAUDE_CREDENTIALS_KV_KEY", "claude-code-credentials"
        )

    async def Process(
        self,
        request_iterator: AsyncIterator[ext_proc_pb2.ProcessingRequest],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[ext_proc_pb2.ProcessingResponse]:
        async for req in request_iterator:
            kind = req.WhichOneof("request")
            if kind == "request_headers":
                yield await self._on_request_headers()
            elif kind == "response_headers":
                yield await self._on_response_headers(req.response_headers)
            else:
                # Body / trailers / unknown: pass through unmodified. We
                # configured the filter to skip body streaming, so the only
                # path that lands here is the trailers message envoy emits
                # at end-of-stream.
                yield ext_proc_pb2.ProcessingResponse()

    async def _on_request_headers(self) -> ext_proc_pb2.ProcessingResponse:
        token = await self._get_access_token()
        mutation = base_pb2.HeaderValueOption(
            header=base_pb2.HeaderValue(key="authorization", raw_value=f"Bearer {token}".encode()),
            append_action=base_pb2.HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD,
        )
        headers_resp = ext_proc_pb2.HeadersResponse(
            response=ext_proc_pb2.CommonResponse(
                header_mutation=ext_proc_pb2.HeaderMutation(
                    set_headers=[mutation],
                    # Whatever the pod sent for x-api-key would conflict
                    # with our Bearer auth and make Anthropic 401. Strip.
                    remove_headers=["x-api-key"],
                ),
            ),
        )
        return ext_proc_pb2.ProcessingResponse(request_headers=headers_resp)

    async def _on_response_headers(
        self, msg: ext_proc_pb2.HttpHeaders
    ) -> ext_proc_pb2.ProcessingResponse:
        status = _peek_status(msg)
        if status == 401:
            # Mark the cached token poisoned so the next request_headers
            # blocks for refresh rather than re-injecting it. Kick the
            # refresh in the background; envoy's retry policy resends the
            # request, and by the time the retry's request_headers callback
            # runs the lock either holds the new token or makes us wait.
            self._access_invalidated = True
            asyncio.create_task(self._refresh())
        return ext_proc_pb2.ProcessingResponse(response_headers=ext_proc_pb2.HeadersResponse())

    async def _get_access_token(self) -> str:
        if self._cached_access is not None and not self._access_invalidated:
            return self._cached_access
        # Either first request after startup (no cache) OR last response
        # was 401 and we need to wait for the refresh task to finish.
        # Acquiring the lock serializes us behind any in-flight refresh.
        async with self._lock:
            if self._cached_access is None:
                self._reload_from_file()
            if self._access_invalidated:
                # Refresh task may have already cleared this; if not, we
                # acquired the lock before it ran. Either way, the lock
                # release means we're past any concurrent _refresh() call.
                pass
        # _reload_from_file may have failed to find a token; surface as a
        # placeholder — Envoy will get 401 from upstream and retry, and
        # the retry path will trigger a refresh.
        return self._cached_access or "missing"

    def _file_expires_at(self, blob: dict[str, Any]) -> int | None:
        """Pull expiresAt (ms) out of a blob; ms-precision matters because
        Anthropic stamps both rotated tokens with the same minute-aligned
        value, so we use it as a freshness comparator."""
        if not isinstance(blob, dict):
            return None
        for k, v in blob.items():
            if k in ("expiresAt", "expires_at") and isinstance(v, int):
                return v
            if isinstance(v, dict):
                found = self._file_expires_at(v)
                if found:
                    return found
        return None

    def _cached_expires_at(self) -> int | None:
        return self._file_expires_at(self._cached_blob) if self._cached_blob else None

    def _reload_from_file(self) -> None:
        """Pull the on-disk blob into the in-memory cache, but only if the
        file is strictly fresher than memory.

        Skipping a stale-file reload is the load-bearing invariant: if we
        just rotated in-process and KV+ESO haven't propagated back yet,
        the file holds pre-rotation tokens whose refresh has already been
        single-use-invalidated by Anthropic. Clobbering memory with that
        would make the next refresh 400 invalid_grant.
        """
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                blob = json.load(f)
        except FileNotFoundError:
            log.error("credentials file %s not found; serving placeholder", CREDENTIALS_FILE)
            return
        except Exception:
            log.exception("could not read credentials file %s", CREDENTIALS_FILE)
            return
        file_access = _walk_for(blob, ("accessToken", "access_token"))
        file_refresh = _walk_for(blob, ("refreshToken", "refresh_token"))
        file_exp = self._file_expires_at(blob)
        cached_exp = self._cached_expires_at()
        if cached_exp is not None and file_exp is not None and file_exp <= cached_exp:
            return  # memory is at least as fresh
        if self._cached_access is not None and file_access == self._cached_access:
            return  # tokens match; nothing to do
        self._cached_blob = blob
        self._cached_access = file_access
        self._cached_refresh = file_refresh
        log.info("loaded credentials from file (access prefix=%s)", (file_access or "")[:12])

    async def _refresh(self) -> None:
        async with self._lock:
            # Re-read the file under the lock: ESO may have mirrored a
            # newer KV value (e.g. someone re-seeded via "+ config sub")
            # and we should prefer that over calling Anthropic ourselves.
            self._reload_from_file()
            if self._cached_refresh is None:
                log.error("no refresh token available; cannot rotate")
                return
            log.info("calling %s to rotate", ANTHROPIC_TOKEN_URL)
            try:
                async with httpx.AsyncClient(timeout=30.0) as http:
                    resp = await http.post(
                        ANTHROPIC_TOKEN_URL,
                        json={
                            "grant_type": "refresh_token",
                            "refresh_token": self._cached_refresh,
                            "client_id": ANTHROPIC_CLIENT_ID,
                        },
                        headers={"Content-Type": "application/json"},
                    )
            except Exception:
                log.exception("refresh request crashed; keeping existing tokens")
                return
            if resp.status_code != 200:
                log.error("refresh failed: status=%s body=%s", resp.status_code, resp.text[:500])
                return
            data = resp.json()
            new_access = data["access_token"]
            new_refresh = data.get("refresh_token") or self._cached_refresh
            expires_in = int(data.get("expires_in", 3600))
            # Update in-memory state FIRST so concurrent waiters see the
            # fresh access token without depending on KV+ESO+kubelet.
            self._cached_access = new_access
            self._cached_refresh = new_refresh
            if self._cached_blob is not None:
                self._cached_blob = _patch_blob(self._cached_blob, new_access, new_refresh, expires_in)
            self._access_invalidated = False
            log.info("rotated successfully (access prefix=%s, expires in %ds)", new_access[:12], expires_in)
            await self._persist_to_kv(expires_in)

    async def _persist_to_kv(self, expires_in: int) -> None:
        """Best-effort write of the rotated blob back to KV.

        Failure mode (KV write errors after a successful Anthropic refresh)
        used to be a chain-killer in the cron design — Anthropic had
        already invalidated the old refresh token, but KV still held it.
        Here it's tolerable: in-memory state already serves the fresh
        access token to ongoing requests, and a future restart (rare,
        and not concurrent with a refresh storm) re-reads from the
        slightly-stale Secret without losing service. ESO will eventually
        re-mirror after the next successful rotation. No alert needed —
        just log and move on.
        """
        if not self._kv_url or self._cached_blob is None:
            return
        try:
            cred = DefaultAzureCredential()
            try:
                async with SecretClient(vault_url=self._kv_url, credential=cred) as kv:
                    await kv.set_secret(self._kv_secret_name, json.dumps(self._cached_blob))
                log.info("wrote rotated blob to %s/%s (expires in %ds)", self._kv_url, self._kv_secret_name, expires_in)
            finally:
                await cred.close()
        except Exception:
            log.exception("KV write failed; tokens stay in memory only")


def _peek_status(msg: ext_proc_pb2.HttpHeaders) -> int | None:
    for h in msg.headers.headers:
        if h.key == ":status":
            value = h.raw_value.decode() if h.raw_value else h.value
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def serve(port: int) -> grpc.aio.Server:
    server = grpc.aio.server()
    ext_proc_grpc.add_ExternalProcessorServicer_to_server(AuthInjector(), server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    await server.start()
    log.info("ext_proc listening on 0.0.0.0:%d", port)
    return server


# Suppress unused-import warning: the http_status import is kept so that
# downstream protobuf descriptor resolution doesn't require eager loading
# from grpc internals if the module is dlopen'd before the deps register.
_ = http_status_pb2  # noqa: F401
