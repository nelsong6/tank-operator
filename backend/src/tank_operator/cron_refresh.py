"""CronJob entrypoint: refresh Anthropic credentials, but only when sessions exist.

Replaces the in-process refresh loop that used to live in sessions.py. That
loop was attractive ("0 rotations on idle clusters, no extra workload") but
exposed every rotation to the orchestrator pod's restart cadence — chart
bumps roll the orchestrator several times a day, and any roll caught between
Anthropic returning 200 and the KV write completing loses the rotated
refresh token forever (Anthropic invalidates the old one, KV still holds it,
all subsequent refreshes 400 with invalid_grant).

This script preserves the idle-skip behaviour by checking the sessions
namespace before calling refresh_now(): if there are no managed Deployments,
exit 0 without touching Anthropic or KV. The CronJob is a singleton at any
point in time (concurrencyPolicy: Forbid) and its lifecycle is decoupled
from the orchestrator's image rollout cadence, so the worst-case race is
narrowed to "kubelet kills the cron pod mid-rotation" — much rarer than
"chart bump rolls the orchestrator pod mid-rotation".

Inline refresh on the 0→1 session transition stays in sessions.py.create():
the user-driven path is rarer than the periodic loop and ensures freshly-
woken-up clusters boot the first session against an up-to-date access token
even if the previous CronJob tick skipped because the cluster was idle.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from kubernetes_asyncio import client, config

from .refresh_credentials import refresh_now

log = logging.getLogger(__name__)

SESSIONS_NAMESPACE = os.environ.get("SESSIONS_NAMESPACE", "tank-operator-sessions")
SESSION_LABEL_SELECTOR = "app.kubernetes.io/managed-by=tank-operator"


async def _has_active_sessions() -> bool:
    """Return True iff at least one tank-operator-managed Deployment exists.

    Uses the same label the orchestrator stamps on every session Deployment
    (sessions.py.SessionManager._deployment_manifest). We don't filter by
    Ready or status — a Deployment that exists is a session the user could
    still be using or waiting on, and we want its access token kept fresh.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()
    api = client.ApiClient()
    try:
        apps = client.AppsV1Api(api)
        deployments = await apps.list_namespaced_deployment(
            namespace=SESSIONS_NAMESPACE,
            label_selector=SESSION_LABEL_SELECTOR,
        )
        return bool(deployments.items)
    finally:
        await api.close()


async def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not await _has_active_sessions():
        log.info("no active sessions in %s; skipping refresh", SESSIONS_NAMESPACE)
        return 0
    log.info("active sessions present; rotating credentials")
    await refresh_now()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
