import asyncio
import contextlib
import hashlib
import logging
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator

from kubernetes_asyncio import client, config

from .exec_proxy import exec_capture

log = logging.getLogger(__name__)

SESSIONS_NAMESPACE = os.environ.get("SESSIONS_NAMESPACE", "tank-operator-sessions")
SESSION_IMAGE = os.environ.get("SESSION_IMAGE", "romainecr.azurecr.io/claude-container:latest")
SESSION_SERVICE_ACCOUNT = os.environ.get("SESSION_SERVICE_ACCOUNT", "claude-session")
GITHUB_APP_SECRET = os.environ.get("GITHUB_APP_SECRET", "github-app-creds")
# OAuth gateway: in-cluster service that impersonates platform.claude.com.
# Session pods reach it via a hostAlias mapping platform.claude.com to this
# Service's ClusterIP — hostAliases requires an IP, not a DNS name, so we
# resolve once at startup and stamp the IP onto every Deployment manifest.
OAUTH_GATEWAY_HOST = os.environ.get(
    "CLAUDE_OAUTH_GATEWAY_HOST",
    "claude-oauth-gateway.tank-operator.svc.cluster.local",
)
OAUTH_GATEWAY_CA_CONFIGMAP = os.environ.get("CLAUDE_OAUTH_GATEWAY_CA_CONFIGMAP", "claude-oauth-ca")
# In-cluster proxy that fronts api.anthropic.com. Same hostAlias trick as
# the OAuth gateway (DNS resolution at orchestrator startup, IP literal
# stamped onto each Deployment manifest). Pods send their requests to
# api.anthropic.com normally; the proxy strips their placeholder
# Authorization header, injects the current real OAuth Bearer, and
# refreshes against platform.claude.com on upstream 401.
API_PROXY_HOST = os.environ.get(
    "CLAUDE_API_PROXY_HOST",
    "claude-api-proxy.tank-operator.svc.cluster.local",
)
# Stamping these on each session Deployment makes ArgoCD claim it into the
# tank-operator-sessions Application's resource tree (visible alongside the
# orchestrator's chart-managed resources). That app has no auto-sync, so
# Argo never tries to reconcile / prune the dynamic deployments — pure
# visualization.
ARGOCD_TRACKING_APP = os.environ.get("ARGOCD_TRACKING_APP", "tank-operator-sessions")
# Reaper config: a session with no open WS for IDLE_TIMEOUT_SECONDS gets
# deleted by the periodic sweep. The 5-min default gives a comfortable
# window for tab reloads / brief network blips while still honoring the
# README's "killed when the tab closes" promise.
IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "300"))
REAPER_INTERVAL_SECONDS = int(os.environ.get("REAPER_INTERVAL_SECONDS", "60"))


class SessionNotFound(Exception):
    pass


class SessionNotOwned(Exception):
    pass


class PodNotReady(Exception):
    pass


SESSION_MODES = ("api_key", "subscription", "config", "remote_control")
DEFAULT_SESSION_MODE = "subscription"
# Config mode: a one-shot pod the user logs into via `claude /login` to seed
# the OAuth credentials in KV. Differs from regular sessions in three ways:
# (1) no credentials are pre-seeded into the pod (we're harvesting, not
# consuming); (2) no platform.claude.com hostAlias override (claude needs to
# reach the real Anthropic for OAuth); (3) no bypassPermissions (the user
# is doing one interactive thing, not running an agent). After the user
# completes /login, the orchestrator's POST /api/sessions/{id}/save-credentials
# reads ~/.claude/.credentials.json out of the pod via exec and writes it to
# Key Vault.
CONFIG_MODE = "config"
# Remote-control mode: the pod runs `claude remote-control` in a tmux window
# so the user can drive sessions from claude.ai/code in their browser instead
# of the in-pod terminal. Otherwise plumbed identically to subscription mode
# (same hostAliases, same cred placeholder). The CLI prints the bridge URL
# only via its TUI, so the bootstrap also passes --debug-file and we read
# the resulting `environment_id=env_XXX` line back via exec to construct
# https://claude.ai/code?environment=<id>.
REMOTE_CONTROL_MODE = "remote_control"
REMOTE_DEBUG_FILE = ".tank/remote-debug.log"
# `tail -1` not `head -1`: if claude remote-control is restarted inside the
# pod (or the same debug file is reused across reconnects) a new
# environment_id line is appended; we want the most recent registration.
_REMOTE_URL_EXTRACT_CMD = (
    f'grep -oE "environment_id=env_[A-Za-z0-9]+" '
    f'"$HOME/{REMOTE_DEBUG_FILE}" 2>/dev/null '
    f'| tail -1 | cut -d= -f2'
)


@dataclass
class SessionInfo:
    id: str
    pod_name: str | None
    owner: str
    status: str
    mode: str
    # Populated only for remote_control sessions once the bridge has
    # registered; None while we're still waiting for the URL to appear.
    remote_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _owner_label(email: str) -> str:
    # K8s label values must match [a-z0-9A-Z._-]{0,63}; email addresses contain `@`.
    digest = hashlib.sha256(email.encode()).hexdigest()[:16]
    return f"u-{digest}"


class SessionManager:
    """Manages session lifecycle as one Deployment per session.

    Deployment was chosen over Job because the workload is long-running until
    explicitly killed (claude CLI inside `sleep infinity`); Jobs are batch
    primitives. Bonus: ArgoCD's resource tree renders Deployments richly
    (ReplicaSet → Pod with health rollup), Jobs do not — useful once we
    surface the sessions namespace as orphaned resources.
    """

    def __init__(self) -> None:
        self._api: client.ApiClient | None = None
        self._apps: client.AppsV1Api | None = None
        self._core: client.CoreV1Api | None = None
        # In-memory connection tracking for the idle reaper. Single replica
        # only (values.yaml pins replicas: 1) — stateful, restart-tolerant
        # via the "adopt with now" branch in _reap_idle.
        self._ws_count: dict[str, int] = {}
        self._activity: dict[str, float] = {}
        self._reaper_task: asyncio.Task[None] | None = None
        # ClusterIP of the OAuth gateway Service — resolved once at startup
        # and stamped onto each Deployment as a hostAlias, since K8s
        # hostAliases require an IP literal, not a DNS name.
        self._oauth_gateway_ip: str | None = None
        # Same idea for the api.anthropic.com proxy — see API_PROXY_HOST.
        self._api_proxy_ip: str | None = None
        # Cache of remote_control bridge URLs. Avoids exec'ing into the pod
        # on every list call once the URL is known. Cleared on session
        # delete; on orchestrator restart we transparently re-discover.
        self._remote_urls: dict[str, str] = {}

    async def startup(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        self._api = client.ApiClient()
        self._apps = client.AppsV1Api(self._api)
        self._core = client.CoreV1Api(self._api)
        self._oauth_gateway_ip = await self._resolve_oauth_gateway_ip()
        self._api_proxy_ip = await self._resolve_service_ip(API_PROXY_HOST, "API proxy")
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _resolve_oauth_gateway_ip(self) -> str | None:
        return await self._resolve_service_ip(OAUTH_GATEWAY_HOST, "OAuth gateway")

    async def _resolve_service_ip(self, host: str, label: str) -> str | None:
        """Resolve an in-cluster Service's ClusterIP via DNS.

        Returns None if resolution fails — callers should treat this as
        "service not deployed yet" and skip stamping the hostAlias
        rather than failing session creation. (Useful for first-install
        or local dev where the chart isn't fully reconciled.)
        """
        try:
            loop = asyncio.get_event_loop()
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            return infos[0][4][0]
        except Exception:
            log.warning("could not resolve %s %s; sessions will boot without it", label, host)
            return None

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
        if self._api is not None:
            await self._api.close()

    def _deployment_manifest(self, session_id: str, owner: str, mode: str) -> dict[str, Any]:
        owner_label = _owner_label(owner)
        selector_labels = {"tank-operator/session-id": session_id}
        deployment_name = f"session-{session_id}"
        argocd_tracking_id = (
            f"{ARGOCD_TRACKING_APP}:apps/Deployment:{SESSIONS_NAMESPACE}/{deployment_name}"
        )
        pod_spec: dict[str, Any] = {
            "serviceAccountName": SESSION_SERVICE_ACCOUNT,
            # The image's USER is claude (uid 1000). Reasserting it here
            # forces the kubelet to reject the pod if the image ever ships
            # back to root, and claude's safety check requires non-root for
            # bypassPermissions mode to take effect.
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
            },
            "containers": [
                # Sidecar: localhost reverse proxy that injects fresh SA-token
                # bearer auth into outbound HTTP MCP calls. Same image as the
                # main container, different command. Required because the
                # projected SA token rotates in-place on disk every ~50min,
                # but env vars set from it at pod start go stale — this proxy
                # reads the file per request so .mcp.json's localhost URLs
                # (see claude-container/mcp.json) get a fresh Bearer every
                # call. See claude-container/mcp-auth-proxy/src/.../server.py.
                {
                    "name": "mcp-auth-proxy",
                    "image": SESSION_IMAGE,
                    "imagePullPolicy": "Always",
                    "command": ["mcp-auth-proxy"],
                },
                {
                    "name": "claude",
                    "image": SESSION_IMAGE,
                    "imagePullPolicy": "Always",
                    "command": ["sleep", "infinity"],
                    "env": [
                        # Read by exec_proxy's bootstrap to pick the
                        # auth path. Sourced at the env level (not
                        # via secret) because the value is per-pod,
                        # not a shared secret.
                        {"name": "TANK_SESSION_MODE", "value": mode},
                        # Force claude (and anything else using the
                        # `supports-hyperlinks` npm lib) to emit OSC 8
                        # hyperlinks. The library's terminal-sniff list
                        # doesn't recognise xterm.js, so without this
                        # claude falls back to plain text URLs and we'd
                        # have to detect wrapped URLs heuristically in
                        # frontend/src/wrappedLinkProvider.ts. With OSC 8
                        # the terminal gets explicit "this byte range is
                        # one link" markers regardless of newlines or
                        # auto-wrap, and xterm.js's built-in OSC 8
                        # support renders them natively.
                        {"name": "FORCE_HYPERLINK", "value": "1"},
                        # Switch claude's TUI to the alternate-screen-buffer
                        # renderer (vim/htop-style) instead of the default
                        # in-place redraw. Fixes the documented Ink
                        # SIGWINCH redraw-leak (anthropics/claude-code#49086)
                        # and full-buffer redraw drift (#29937) — both of
                        # which manifest as ghost lines and post-resize text
                        # collisions in xterm.js, since xterm.js is the same
                        # rendering-throughput-bound consumer class as the
                        # VS Code integrated terminal that the docs call out.
                        {"name": "CLAUDE_CODE_NO_FLICKER", "value": "1"},
                    ],
                    "envFrom": [
                        {"secretRef": {"name": GITHUB_APP_SECRET}},
                    ],
                    "stdin": True,
                    "tty": True,
                }
            ],
        }
        # OAuth gateway plumbing: add a hostAlias so platform.claude.com
        # resolves to the in-cluster gateway Service, mount the gateway's
        # CA cert (NOT the private key — that stays in the orchestrator
        # namespace), and set NODE_EXTRA_CA_CERTS so claude's Node runtime
        # trusts it. If the gateway IP couldn't be resolved at startup we
        # skip this whole stanza; the pod will boot but won't be able to
        # refresh — surfaces as a 401 the user can recover from by
        # recreating the session once the gateway is healthy.
        #
        # Config mode skips this entirely: the user is about to do `claude
        # /login`, which has to reach the REAL platform.claude.com to
        # complete OAuth. Pointing it at our in-cluster gateway would make
        # the auth endpoints 404.
        if mode != CONFIG_MODE and (self._oauth_gateway_ip or self._api_proxy_ip):
            host_aliases: list[dict[str, Any]] = []
            if self._oauth_gateway_ip:
                host_aliases.append(
                    {"ip": self._oauth_gateway_ip, "hostnames": ["platform.claude.com"]}
                )
            # api.anthropic.com is hijacked to the in-cluster proxy. The
            # proxy's leaf cert is signed by the same `claude-oauth-ca` the
            # session pod already trusts via NODE_EXTRA_CA_CERTS, so no
            # extra trust-store wiring is needed.
            if self._api_proxy_ip:
                host_aliases.append(
                    {"ip": self._api_proxy_ip, "hostnames": ["api.anthropic.com"]}
                )
            pod_spec["hostAliases"] = host_aliases
            # Index by name, not position — the sidecar lives at [0] now,
            # but only the claude container needs the OAuth gateway CA.
            container = next(c for c in pod_spec["containers"] if c["name"] == "claude")
            container["env"].append(
                {"name": "NODE_EXTRA_CA_CERTS", "value": "/etc/oauth-gateway-ca/ca.crt"}
            )
            container["volumeMounts"] = [
                {
                    "name": "oauth-gateway-ca",
                    "mountPath": "/etc/oauth-gateway-ca",
                    "readOnly": True,
                }
            ]
            pod_spec["volumes"] = [
                {
                    "name": "oauth-gateway-ca",
                    "configMap": {"name": OAUTH_GATEWAY_CA_CONFIGMAP},
                }
            ]
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": deployment_name,
                "namespace": SESSIONS_NAMESPACE,
                "labels": {
                    "app.kubernetes.io/managed-by": "tank-operator",
                    "app.kubernetes.io/instance": ARGOCD_TRACKING_APP,
                    "tank-operator/owner": owner_label,
                    "tank-operator/session-id": session_id,
                    "tank-operator/mode": mode,
                },
                "annotations": {
                    "tank-operator/owner-email": owner,
                    "argocd.argoproj.io/tracking-id": argocd_tracking_id,
                },
            },
            "spec": {
                "replicas": 1,
                # No old ReplicaSets — this Deployment is never updated, only
                # created and deleted, so history is just clutter in Argo.
                "revisionHistoryLimit": 0,
                "selector": {"matchLabels": selector_labels},
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/managed-by": "tank-operator",
                            "tank-operator/owner": owner_label,
                            "tank-operator/session-id": session_id,
                            "tank-operator/mode": mode,
                            "azure.workload.identity/use": "true",
                        },
                    },
                    "spec": pod_spec,
                },
            },
        }

    async def create(self, owner: str, mode: str = DEFAULT_SESSION_MODE) -> SessionInfo:
        assert self._apps is not None
        if mode not in SESSION_MODES:
            raise ValueError(f"unknown session mode: {mode!r}")
        # Lazy retry of in-cluster Service resolution — handles the
        # chart-install race where the orchestrator pod starts before its
        # sibling Services exist. After first success the IP is cached;
        # if a Service is ever recreated (rare), restart the orchestrator.
        if self._oauth_gateway_ip is None:
            self._oauth_gateway_ip = await self._resolve_oauth_gateway_ip()
        if self._api_proxy_ip is None:
            self._api_proxy_ip = await self._resolve_service_ip(API_PROXY_HOST, "API proxy")
        # No credential refresh on the create path: the api-proxy
        # (api-proxy/src/tank_api_proxy/server.py) owns rotation now,
        # triggered by upstream 401s on real api.anthropic.com calls.
        # Session pods carry a placeholder Bearer; the proxy strips it
        # and injects the real one, refreshing against platform.claude.com
        # behind the scenes when it observes a 401.
        session_id = uuid.uuid4().hex[:10]
        await self._apps.create_namespaced_deployment(
            namespace=SESSIONS_NAMESPACE,
            body=self._deployment_manifest(session_id, owner, mode),
        )
        # Seed activity so the reaper gives the session a full
        # IDLE_TIMEOUT to receive its first WS before being eligible
        # for deletion.
        self._activity[session_id] = time.monotonic()
        self._ws_count[session_id] = 0
        return SessionInfo(id=session_id, pod_name=None, owner=owner, status="Pending", mode=mode)

    async def list(self, owner: str) -> list[SessionInfo]:
        assert self._apps is not None and self._core is not None
        owner_label = _owner_label(owner)
        # Fetch deployments + pods together so we can resolve pod names for
        # remote-control URL discovery without a 90s wait-for-Ready loop.
        deployments_task = self._apps.list_namespaced_deployment(
            namespace=SESSIONS_NAMESPACE,
            label_selector=f"tank-operator/owner={owner_label}",
        )
        pods_task = self._core.list_namespaced_pod(
            namespace=SESSIONS_NAMESPACE,
            label_selector=f"tank-operator/owner={owner_label}",
        )
        deployments, pods = await asyncio.gather(deployments_task, pods_task)
        ready_pods: dict[str, str] = {}
        for pod in pods.items:
            session_id = pod.metadata.labels.get("tank-operator/session-id")
            if session_id and _pod_ready(pod):
                ready_pods[session_id] = pod.metadata.name

        infos: list[SessionInfo] = []
        url_lookups: list[tuple[int, str, str]] = []
        for d in deployments.items:
            session_id = d.metadata.labels.get(
                "tank-operator/session-id", d.metadata.name
            )
            mode = d.metadata.labels.get("tank-operator/mode", DEFAULT_SESSION_MODE)
            info = SessionInfo(
                id=session_id,
                pod_name=None,
                owner=owner,
                status=_deployment_status(d),
                mode=mode,
                remote_url=self._remote_urls.get(session_id) if mode == REMOTE_CONTROL_MODE else None,
            )
            infos.append(info)
            if (
                mode == REMOTE_CONTROL_MODE
                and info.remote_url is None
                and session_id in ready_pods
            ):
                url_lookups.append((len(infos) - 1, session_id, ready_pods[session_id]))

        # Resolve any missing remote_control URLs concurrently. Each lookup
        # is one exec_capture into the pod; cached on success so this only
        # runs in the brief window between pod-Ready and URL-discovered.
        if url_lookups:
            results = await asyncio.gather(
                *(self._fetch_remote_url(pod_name) for _, _, pod_name in url_lookups),
                return_exceptions=True,
            )
            for (idx, session_id, _), url in zip(url_lookups, results):
                if isinstance(url, str) and url:
                    self._remote_urls[session_id] = url
                    infos[idx].remote_url = url
        return infos

    async def _fetch_remote_url(self, pod_name: str) -> str | None:
        """Read the bridge environment_id out of the pod's debug log and
        format the corresponding claude.ai/code URL. Returns None until
        `claude remote-control` has registered — typically <1s after the
        process starts, but the file may not exist yet on the first poll.
        """
        try:
            raw = await exec_capture(
                SESSIONS_NAMESPACE,
                pod_name,
                ["sh", "-c", _REMOTE_URL_EXTRACT_CMD],
            )
        except Exception:
            log.exception("remote-control URL extract failed for pod %s", pod_name)
            return None
        env_id = raw.decode(errors="replace").strip()
        if not env_id.startswith("env_"):
            return None
        return f"https://claude.ai/code?environment={env_id}"

    async def get_session(self, owner: str, session_id: str) -> SessionInfo:
        """Look up a single session by id, verifying ownership.

        Cheaper than get_pod_name because it doesn't wait for pod-Ready —
        just reads the Deployment to get mode/status. Use this when you
        only need session metadata (e.g. checking mode before allowing an
        action), and get_pod_name when you need to actually exec into the
        pod.
        """
        assert self._apps is not None
        owner_label = _owner_label(owner)
        try:
            deployment = await self._apps.read_namespaced_deployment(
                name=f"session-{session_id}", namespace=SESSIONS_NAMESPACE
            )
        except client.ApiException as e:
            if e.status == 404:
                raise SessionNotFound(session_id) from e
            raise
        if deployment.metadata.labels.get("tank-operator/owner") != owner_label:
            raise SessionNotOwned(session_id)
        mode = deployment.metadata.labels.get("tank-operator/mode", DEFAULT_SESSION_MODE)
        return SessionInfo(
            id=session_id,
            pod_name=None,
            owner=owner,
            status=_deployment_status(deployment),
            mode=mode,
            remote_url=self._remote_urls.get(session_id) if mode == REMOTE_CONTROL_MODE else None,
        )

    async def get_pod_name(self, owner: str, session_id: str, timeout: float = 90.0) -> str:
        """Look up the pod backing a session, waiting up to `timeout` seconds for it to be Ready."""
        assert self._apps is not None and self._core is not None
        owner_label = _owner_label(owner)
        name = f"session-{session_id}"
        try:
            deployment = await self._apps.read_namespaced_deployment(
                name=name, namespace=SESSIONS_NAMESPACE
            )
        except client.ApiException as e:
            if e.status == 404:
                raise SessionNotFound(session_id) from e
            raise
        if deployment.metadata.labels.get("tank-operator/owner") != owner_label:
            raise SessionNotOwned(session_id)

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            pods = await self._core.list_namespaced_pod(
                namespace=SESSIONS_NAMESPACE,
                label_selector=f"tank-operator/session-id={session_id}",
            )
            for pod in pods.items:
                if _pod_ready(pod):
                    return pod.metadata.name
            await asyncio.sleep(1)
        raise PodNotReady(session_id)

    async def delete(self, owner: str, session_id: str) -> None:
        assert self._apps is not None
        owner_label = _owner_label(owner)
        name = f"session-{session_id}"
        try:
            deployment = await self._apps.read_namespaced_deployment(
                name=name, namespace=SESSIONS_NAMESPACE
            )
        except client.ApiException as e:
            if e.status == 404:
                raise SessionNotFound(session_id) from e
            raise
        if deployment.metadata.labels.get("tank-operator/owner") != owner_label:
            raise SessionNotOwned(session_id)
        await self._apps.delete_namespaced_deployment(
            name=name,
            namespace=SESSIONS_NAMESPACE,
            propagation_policy="Foreground",
        )
        self._ws_count.pop(session_id, None)
        self._activity.pop(session_id, None)
        self._remote_urls.pop(session_id, None)

    @contextlib.asynccontextmanager
    async def track_ws(self, session_id: str) -> AsyncIterator[None]:
        """Increment the WS counter for the lifetime of the bridge.

        The reaper treats a session with `_ws_count > 0` as live; on exit we
        bump `_activity` so the IDLE_TIMEOUT clock starts from disconnect,
        not from the last sweep.
        """
        self._ws_count[session_id] = self._ws_count.get(session_id, 0) + 1
        self._activity[session_id] = time.monotonic()
        try:
            yield
        finally:
            self._ws_count[session_id] = max(0, self._ws_count.get(session_id, 1) - 1)
            self._activity[session_id] = time.monotonic()

    async def _reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(REAPER_INTERVAL_SECONDS)
                await self._reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reaper sweep failed")

    async def _reap_idle(self) -> None:
        assert self._apps is not None
        now = time.monotonic()
        deployments = await self._apps.list_namespaced_deployment(
            namespace=SESSIONS_NAMESPACE,
            label_selector="app.kubernetes.io/managed-by=tank-operator",
        )
        for d in deployments.items:
            session_id = d.metadata.labels.get("tank-operator/session-id")
            if not session_id:
                continue
            if self._ws_count.get(session_id, 0) > 0:
                # Live connection — keep the activity clock current.
                self._activity[session_id] = now
                continue
            last = self._activity.get(session_id)
            if last is None:
                # Orchestrator restart: we don't know how long this session
                # has been idle. Adopt now; the next sweep that finds it
                # still idle will reap.
                self._activity[session_id] = now
                continue
            if now - last < IDLE_TIMEOUT_SECONDS:
                continue
            log.info("reaping idle session %s (idle %.0fs)", session_id, now - last)
            try:
                await self._apps.delete_namespaced_deployment(
                    name=d.metadata.name,
                    namespace=SESSIONS_NAMESPACE,
                    propagation_policy="Foreground",
                )
            except client.ApiException:
                log.exception("failed to delete idle session %s", session_id)
                continue
            self._ws_count.pop(session_id, None)
            self._activity.pop(session_id, None)
            self._remote_urls.pop(session_id, None)


def _deployment_status(deployment: Any) -> str:
    """Map a Deployment's status to the same vocabulary the frontend already uses."""
    status = deployment.status
    if status is None:
        return "Pending"
    ready = status.ready_replicas or 0
    if ready >= 1:
        return "Active"
    if status.conditions:
        for c in status.conditions:
            # ReplicaFailure or Progressing=False with reason ProgressDeadlineExceeded
            # both indicate the rollout has given up. Treat as Failed so the UI
            # shows red and the user can delete + retry.
            if c.type == "ReplicaFailure" and c.status == "True":
                return "Failed"
            if c.type == "Progressing" and c.status == "False":
                return "Failed"
    return "Pending"


def _pod_ready(pod: Any) -> bool:
    if not pod.status or pod.status.phase != "Running":
        return False
    statuses = pod.status.container_statuses or []
    return bool(statuses) and all(cs.ready for cs in statuses)
