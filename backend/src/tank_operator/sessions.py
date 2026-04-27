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

from .refresh_credentials import refresh_now

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
# Anthropic OAuth refresh cadence WHILE sessions exist. Idle clusters do
# zero rotations — the timer is started on the 0 → 1 session transition
# and cancelled on 1 → 0. 30m matches the prior CronJob cadence and gives
# a comfortable buffer under typical 1h access-token TTLs.
CREDENTIAL_REFRESH_INTERVAL_SECONDS = int(
    os.environ.get("CREDENTIAL_REFRESH_INTERVAL_SECONDS", "1800")
)


class SessionNotFound(Exception):
    pass


class SessionNotOwned(Exception):
    pass


class PodNotReady(Exception):
    pass


SESSION_MODES = ("api_key", "subscription", "config")
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


@dataclass
class SessionInfo:
    id: str
    pod_name: str | None
    owner: str
    status: str
    mode: str

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
        # Periodic credential rotation, only running while at least one
        # session exists. See _ensure_credential_refresh_running and
        # _maybe_stop_credential_refresh.
        self._credential_refresh_task: asyncio.Task[None] | None = None
        # Serialize lifecycle hooks (create/delete) so concurrent calls
        # can't both observe the same 0 → 1 transition (would refresh
        # twice) or 1 → 0 (would race the timer cancel against itself).
        self._lifecycle_lock = asyncio.Lock()
        # ClusterIP of the OAuth gateway Service — resolved once at startup
        # and stamped onto each Deployment as a hostAlias, since K8s
        # hostAliases require an IP literal, not a DNS name.
        self._oauth_gateway_ip: str | None = None

    async def startup(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        self._api = client.ApiClient()
        self._apps = client.AppsV1Api(self._api)
        self._core = client.CoreV1Api(self._api)
        self._oauth_gateway_ip = await self._resolve_oauth_gateway_ip()
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _resolve_oauth_gateway_ip(self) -> str | None:
        """Resolve the OAuth gateway Service's ClusterIP via cluster DNS.

        Returns None if resolution fails — callers should treat this as
        "OAuth gateway not deployed yet" and skip stamping the hostAlias
        rather than failing session creation. (Useful for first-install or
        local dev where the chart isn't fully reconciled.)
        """
        try:
            loop = asyncio.get_event_loop()
            infos = await loop.getaddrinfo(OAUTH_GATEWAY_HOST, None, type=socket.SOCK_STREAM)
            return infos[0][4][0]
        except Exception:
            log.warning("could not resolve OAuth gateway %s; sessions will boot without it", OAUTH_GATEWAY_HOST)
            return None

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
        if self._credential_refresh_task is not None:
            self._credential_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._credential_refresh_task
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
        if self._oauth_gateway_ip and mode != CONFIG_MODE:
            pod_spec["hostAliases"] = [
                {"ip": self._oauth_gateway_ip, "hostnames": ["platform.claude.com"]}
            ]
            container = pod_spec["containers"][0]
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
        # Lazy retry of OAuth gateway resolution — handles the chart-install
        # race where the orchestrator pod starts before its sibling Service
        # exists. After first successful resolution the IP is cached; if the
        # Service is ever recreated (rare), restart the orchestrator.
        if self._oauth_gateway_ip is None:
            self._oauth_gateway_ip = await self._resolve_oauth_gateway_ip()
        async with self._lifecycle_lock:
            # 0 → 1 transition for non-config sessions: rotate credentials
            # synchronously so the new session boots against a freshly-
            # refreshed access token. Config-mode sessions skip this — they
            # exist to seed credentials, not consume them, and refreshing
            # against an invalid refresh token here would fail the user's
            # recovery flow.
            should_kick_refresh = (
                mode != CONFIG_MODE and not self._activity
            )
            session_id = uuid.uuid4().hex[:10]
            if should_kick_refresh:
                try:
                    await refresh_now()
                except Exception:
                    log.exception(
                        "on-create credential refresh failed; session will boot "
                        "with whatever's currently in KV — re-seed via + config sub "
                        "if claude can't authenticate"
                    )
            await self._apps.create_namespaced_deployment(
                namespace=SESSIONS_NAMESPACE,
                body=self._deployment_manifest(session_id, owner, mode),
            )
            # Seed activity so the reaper gives the session a full
            # IDLE_TIMEOUT to receive its first WS before being eligible
            # for deletion.
            self._activity[session_id] = time.monotonic()
            self._ws_count[session_id] = 0
            if mode != CONFIG_MODE:
                self._ensure_credential_refresh_running()
        return SessionInfo(id=session_id, pod_name=None, owner=owner, status="Pending", mode=mode)

    def _ensure_credential_refresh_running(self) -> None:
        """Start the periodic credential-refresh task if not already running.

        Idempotent — safe to call on every session create. Combined with
        _maybe_stop_credential_refresh() on delete, this gives "rotation
        runs only while sessions exist" behaviour: idle clusters do zero
        rotations.
        """
        if self._credential_refresh_task is None or self._credential_refresh_task.done():
            self._credential_refresh_task = asyncio.create_task(
                self._credential_refresh_loop()
            )

    def _maybe_stop_credential_refresh(self) -> None:
        """Cancel the periodic refresh task if no sessions remain.

        Called from session delete paths after _activity has been pruned.
        Doesn't await the cancellation — the task observes CancelledError
        on its next sleep boundary, which is fine since rotation has no
        side effects in flight.
        """
        if not self._activity and self._credential_refresh_task is not None:
            if not self._credential_refresh_task.done():
                self._credential_refresh_task.cancel()
            self._credential_refresh_task = None

    async def _credential_refresh_loop(self) -> None:
        """Refresh credentials every CREDENTIAL_REFRESH_INTERVAL_SECONDS.

        The first refresh on the 0 → 1 transition is done inline in
        create() so the new session boots against fresh credentials; this
        loop covers sessions that outlive the access-token TTL.
        """
        while True:
            try:
                await asyncio.sleep(CREDENTIAL_REFRESH_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                log.info("credential refresh loop cancelled (no sessions)")
                raise
            try:
                log.info("periodic credential refresh tick")
                await refresh_now()
            except Exception:
                log.exception("periodic credential refresh failed; will retry next tick")

    async def list(self, owner: str) -> list[SessionInfo]:
        assert self._apps is not None
        owner_label = _owner_label(owner)
        deployments = await self._apps.list_namespaced_deployment(
            namespace=SESSIONS_NAMESPACE,
            label_selector=f"tank-operator/owner={owner_label}",
        )
        return [
            SessionInfo(
                id=d.metadata.labels.get("tank-operator/session-id", d.metadata.name),
                pod_name=None,
                owner=owner,
                status=_deployment_status(d),
                mode=d.metadata.labels.get("tank-operator/mode", DEFAULT_SESSION_MODE),
            )
            for d in deployments.items
        ]

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
        return SessionInfo(
            id=session_id,
            pod_name=None,
            owner=owner,
            status=_deployment_status(deployment),
            mode=deployment.metadata.labels.get("tank-operator/mode", DEFAULT_SESSION_MODE),
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
        async with self._lifecycle_lock:
            await self._apps.delete_namespaced_deployment(
                name=name,
                namespace=SESSIONS_NAMESPACE,
                propagation_policy="Foreground",
            )
            self._ws_count.pop(session_id, None)
            self._activity.pop(session_id, None)
            self._maybe_stop_credential_refresh()

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
        # If the sweep emptied _activity (or the orchestrator just started
        # with no sessions), stop the periodic refresh task. Conversely,
        # if it adopted existing sessions on cold start, ensure the task
        # is running so they get periodic rotations as long as they live.
        if self._activity:
            self._ensure_credential_refresh_running()
        else:
            self._maybe_stop_credential_refresh()


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
