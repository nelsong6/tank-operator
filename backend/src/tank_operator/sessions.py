import asyncio
import contextlib
import hashlib
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator

from kubernetes_asyncio import client, config

log = logging.getLogger(__name__)

SESSIONS_NAMESPACE = os.environ.get("SESSIONS_NAMESPACE", "tank-operator-sessions")
SESSION_IMAGE = os.environ.get("SESSION_IMAGE", "romainecr.azurecr.io/claude-container:latest")
SESSION_SERVICE_ACCOUNT = os.environ.get("SESSION_SERVICE_ACCOUNT", "claude-session")
GITHUB_APP_SECRET = os.environ.get("GITHUB_APP_SECRET", "github-app-creds")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "60"))
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


@dataclass
class SessionInfo:
    id: str
    pod_name: str | None
    owner: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _owner_label(email: str) -> str:
    # K8s label values must match [a-z0-9A-Z._-]{0,63}; email addresses contain `@`.
    digest = hashlib.sha256(email.encode()).hexdigest()[:16]
    return f"u-{digest}"


class SessionManager:
    def __init__(self) -> None:
        self._api: client.ApiClient | None = None
        self._batch: client.BatchV1Api | None = None
        self._core: client.CoreV1Api | None = None
        # In-memory connection tracking for the idle reaper. Single replica
        # only (values.yaml pins replicas: 1) — stateful, restart-tolerant
        # via the "adopt with now" branch in _reap_idle.
        self._ws_count: dict[str, int] = {}
        self._activity: dict[str, float] = {}
        self._reaper_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        self._api = client.ApiClient()
        self._batch = client.BatchV1Api(self._api)
        self._core = client.CoreV1Api(self._api)
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
        if self._api is not None:
            await self._api.close()

    def _job_manifest(self, session_id: str, owner: str) -> dict[str, Any]:
        owner_label = _owner_label(owner)
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"session-{session_id}",
                "namespace": SESSIONS_NAMESPACE,
                "labels": {
                    "app.kubernetes.io/managed-by": "tank-operator",
                    "tank-operator/owner": owner_label,
                    "tank-operator/session-id": session_id,
                },
                "annotations": {
                    "tank-operator/owner-email": owner,
                },
            },
            "spec": {
                "ttlSecondsAfterFinished": SESSION_TTL_SECONDS,
                "backoffLimit": 0,
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/managed-by": "tank-operator",
                            "tank-operator/owner": owner_label,
                            "tank-operator/session-id": session_id,
                            "azure.workload.identity/use": "true",
                        },
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": SESSION_SERVICE_ACCOUNT,
                        "containers": [
                            {
                                "name": "claude",
                                "image": SESSION_IMAGE,
                                "imagePullPolicy": "Always",
                                "command": ["sleep", "infinity"],
                                "envFrom": [
                                    {"secretRef": {"name": GITHUB_APP_SECRET}},
                                ],
                                "stdin": True,
                                "tty": True,
                            }
                        ],
                    },
                },
            },
        }

    async def create(self, owner: str) -> SessionInfo:
        assert self._batch is not None
        session_id = uuid.uuid4().hex[:10]
        await self._batch.create_namespaced_job(
            namespace=SESSIONS_NAMESPACE,
            body=self._job_manifest(session_id, owner),
        )
        # Seed activity so the reaper gives the session a full IDLE_TIMEOUT
        # to receive its first WS before being eligible for deletion.
        self._activity[session_id] = time.monotonic()
        self._ws_count[session_id] = 0
        return SessionInfo(id=session_id, pod_name=None, owner=owner, status="Pending")

    async def list(self, owner: str) -> list[SessionInfo]:
        assert self._batch is not None
        owner_label = _owner_label(owner)
        jobs = await self._batch.list_namespaced_job(
            namespace=SESSIONS_NAMESPACE,
            label_selector=f"tank-operator/owner={owner_label}",
        )
        return [
            SessionInfo(
                id=j.metadata.labels.get("tank-operator/session-id", j.metadata.name),
                pod_name=None,
                owner=owner,
                status=_job_status(j),
            )
            for j in jobs.items
        ]

    async def get_pod_name(self, owner: str, session_id: str, timeout: float = 90.0) -> str:
        """Look up the pod backing a session, waiting up to `timeout` seconds for it to be Running."""
        assert self._batch is not None and self._core is not None
        owner_label = _owner_label(owner)
        name = f"session-{session_id}"
        try:
            job = await self._batch.read_namespaced_job(name=name, namespace=SESSIONS_NAMESPACE)
        except client.ApiException as e:
            if e.status == 404:
                raise SessionNotFound(session_id) from e
            raise
        if job.metadata.labels.get("tank-operator/owner") != owner_label:
            raise SessionNotOwned(session_id)

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            pods = await self._core.list_namespaced_pod(
                namespace=SESSIONS_NAMESPACE,
                label_selector=f"tank-operator/session-id={session_id}",
            )
            for pod in pods.items:
                if pod.status and pod.status.phase == "Running":
                    return pod.metadata.name
            await asyncio.sleep(1)
        raise PodNotReady(session_id)

    async def delete(self, owner: str, session_id: str) -> None:
        assert self._batch is not None
        owner_label = _owner_label(owner)
        name = f"session-{session_id}"
        try:
            job = await self._batch.read_namespaced_job(name=name, namespace=SESSIONS_NAMESPACE)
        except client.ApiException as e:
            if e.status == 404:
                raise SessionNotFound(session_id) from e
            raise
        if job.metadata.labels.get("tank-operator/owner") != owner_label:
            raise SessionNotOwned(session_id)
        await self._batch.delete_namespaced_job(
            name=name,
            namespace=SESSIONS_NAMESPACE,
            propagation_policy="Foreground",
        )
        self._ws_count.pop(session_id, None)
        self._activity.pop(session_id, None)

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
        assert self._batch is not None
        now = time.monotonic()
        jobs = await self._batch.list_namespaced_job(
            namespace=SESSIONS_NAMESPACE,
            label_selector="app.kubernetes.io/managed-by=tank-operator",
        )
        for job in jobs.items:
            session_id = job.metadata.labels.get("tank-operator/session-id")
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
                await self._batch.delete_namespaced_job(
                    name=job.metadata.name,
                    namespace=SESSIONS_NAMESPACE,
                    propagation_policy="Foreground",
                )
            except client.ApiException:
                log.exception("failed to delete idle session %s", session_id)
                continue
            self._ws_count.pop(session_id, None)
            self._activity.pop(session_id, None)


def _job_status(job: Any) -> str:
    if job.status is None:
        return "Pending"
    if job.status.active:
        return "Active"
    if job.status.succeeded:
        return "Succeeded"
    if job.status.failed:
        return "Failed"
    return "Pending"
