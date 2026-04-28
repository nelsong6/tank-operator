"""Read-only ArgoCD REST tools.

Defense in depth: tool surface is constrained to GET endpoints + the
`/sync` action. argocd-rbac-cm grants the MCP's SA exactly those verbs
(applications get/sync, projects get, repositories get, clusters get) — so
even if a tool wrapper were bypassed, the bearer can't write anything else.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .dex import ARGOCD_SERVER_URL, get_bearer


_TIMEOUT_SECONDS = 30


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=ARGOCD_SERVER_URL,
        headers={"Authorization": f"Bearer {get_bearer()}"},
        timeout=_TIMEOUT_SECONDS,
    )


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    with _client() as c:
        resp = c.get(path, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"ArgoCD GET {path} -> {resp.status_code} {resp.text}")
    return resp.json()


def _post(path: str, json_body: dict[str, Any] | None = None) -> Any:
    with _client() as c:
        resp = c.post(path, json=json_body)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"ArgoCD POST {path} -> {resp.status_code} {resp.text}")
    return resp.json() if resp.text else {}


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_applications(
        project: str | None = None,
        selector: str | None = None,
    ) -> list[dict[str, Any]]:
        """List ArgoCD Applications. `project` filters by AppProject;
        `selector` is a label selector ('app=foo,role=bar')."""
        params: dict[str, Any] = {}
        if project:
            params["projects"] = project
        if selector:
            params["selector"] = selector
        body = _get("/api/v1/applications", params=params)
        out = []
        for app in body.get("items") or []:
            md = app.get("metadata", {})
            sp = app.get("spec", {})
            st = app.get("status", {})
            out.append(
                {
                    "name": md.get("name"),
                    "namespace": md.get("namespace"),
                    "project": sp.get("project"),
                    "destination": sp.get("destination"),
                    "source": sp.get("source"),
                    "syncStatus": st.get("sync", {}).get("status"),
                    "healthStatus": st.get("health", {}).get("status"),
                    "revision": st.get("sync", {}).get("revision"),
                }
            )
        return out

    @mcp.tool()
    def get_application(name: str) -> dict[str, Any]:
        """Return the full Application object including spec + status +
        operationState. Use this when list_applications doesn't have the
        detail you need (resource tree, sync result, conditions)."""
        return _get(f"/api/v1/applications/{name}")

    @mcp.tool()
    def get_application_resource_tree(name: str) -> dict[str, Any]:
        """Return the live resource tree for an Application — every
        K8s object ArgoCD is tracking, with health + sync per node. Useful
        for diagnosing why an app is Degraded without pulling each
        resource by hand."""
        return _get(f"/api/v1/applications/{name}/resource-tree")

    @mcp.tool()
    def get_application_managed_resources(name: str) -> dict[str, Any]:
        """Return live-vs-target diffs for each managed resource. This is
        what the UI's "App Diff" view uses."""
        return _get(f"/api/v1/applications/{name}/managed-resources")

    @mcp.tool()
    def get_application_events(name: str) -> dict[str, Any]:
        """Return events ArgoCD has recorded for the Application — sync
        operations, health transitions, hook execution."""
        return _get(f"/api/v1/applications/{name}/events")

    @mcp.tool()
    def sync_application(
        name: str,
        revision: str | None = None,
        prune: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Trigger an ArgoCD sync. revision defaults to the Application's
        configured targetRevision. dry_run=True returns the diff without
        applying. prune=True deletes resources removed from git — leave
        False unless you specifically want a destructive sync."""
        body: dict[str, Any] = {"prune": prune, "dryRun": dry_run}
        if revision:
            body["revision"] = revision
        return _post(f"/api/v1/applications/{name}/sync", json_body=body)

    @mcp.tool()
    def list_projects() -> list[dict[str, Any]]:
        """List AppProjects."""
        body = _get("/api/v1/projects")
        return [
            {
                "name": p.get("metadata", {}).get("name"),
                "description": p.get("spec", {}).get("description"),
                "sourceRepos": p.get("spec", {}).get("sourceRepos"),
                "destinations": p.get("spec", {}).get("destinations"),
            }
            for p in (body.get("items") or [])
        ]

    @mcp.tool()
    def list_repositories() -> list[dict[str, Any]]:
        """List configured repositories. Connection state included so you
        can spot a repo whose creds rotted."""
        body = _get("/api/v1/repositories")
        return [
            {
                "repo": r.get("repo"),
                "type": r.get("type"),
                "name": r.get("name"),
                "connectionState": r.get("connectionState", {}).get("status"),
                "connectionMessage": r.get("connectionState", {}).get("message"),
            }
            for r in (body.get("items") or [])
        ]

    @mcp.tool()
    def list_clusters() -> list[dict[str, Any]]:
        """List clusters ArgoCD knows about. In-cluster (kubernetes.default.svc)
        is always present; remote clusters appear here once registered."""
        body = _get("/api/v1/clusters")
        return [
            {
                "name": c.get("name"),
                "server": c.get("server"),
                "connectionState": c.get("connectionState", {}).get("status"),
                "serverVersion": c.get("serverVersion"),
            }
            for c in (body.get("items") or [])
        ]

    @mcp.tool()
    def server_version() -> dict[str, Any]:
        """Return ArgoCD server version info — handy when comparing API
        behaviour across upgrades."""
        return _get("/api/version")
