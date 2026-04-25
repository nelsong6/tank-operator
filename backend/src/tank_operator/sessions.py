import asyncio
import hashlib
import os
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from kubernetes_asyncio import client, config

SESSIONS_NAMESPACE = os.environ.get("SESSIONS_NAMESPACE", "tank-operator-sessions")
SESSION_IMAGE = os.environ.get("SESSION_IMAGE", "romainecr.azurecr.io/claude-container:latest")
SESSION_SERVICE_ACCOUNT = os.environ.get("SESSION_SERVICE_ACCOUNT", "claude-session")
GITHUB_APP_SECRET = os.environ.get("GITHUB_APP_SECRET", "github-app-creds")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "60"))


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

    async def startup(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        self._api = client.ApiClient()
        self._batch = client.BatchV1Api(self._api)
        self._core = client.CoreV1Api(self._api)

    async def shutdown(self) -> None:
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
