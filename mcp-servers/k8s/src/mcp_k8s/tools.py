"""kubectl + helm tools.

Defense in depth: every command is a hard-coded subcommand (get, describe,
logs, top, helm get/list/status/history). The pod's SA gets read-mostly
RBAC — these wrappers keep the tool surface aligned so the agent can't
accidentally request something the SA isn't permitted to do.

Two intentional write verbs:
  - delete_pod — useful when a controller is wedged but its parent
    StatefulSet/Deployment is fine. Pod deletion is recoverable: the
    parent recreates it.
  - rollout_restart — patches a workload's pod-template annotation to
    trigger a rolling restart. Same semantics as
    `kubectl rollout restart`.

Both are paired with explicit RBAC additions in
infra-bootstrap/k8s-mcp-k8s/templates/cluster-reader.yaml.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from mcp.server.fastmcp import FastMCP


_TIMEOUT_SECONDS = 30


def _run(cmd: list[str], *, parse_json: bool = False) -> Any:
    """Run a binary, return stdout. Surfaces stderr to the caller on failure."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {_TIMEOUT_SECONDS}s: {' '.join(cmd)}") from exc

    if proc.returncode != 0:
        # stderr usually carries the useful diagnostic; stdout is rarely set
        # on failure. Strip both so the agent sees a clean message.
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {msg}")

    if parse_json:
        return json.loads(proc.stdout)
    return proc.stdout


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_namespaces() -> list[dict[str, Any]]:
        """List all namespaces in the cluster."""
        body = _run(["kubectl", "get", "namespaces", "-o", "json"], parse_json=True)
        return [
            {
                "name": item["metadata"]["name"],
                "phase": item.get("status", {}).get("phase"),
                "created": item["metadata"].get("creationTimestamp"),
            }
            for item in body.get("items", [])
        ]

    @mcp.tool()
    def list_resources(
        kind: str,
        namespace: str | None = None,
        all_namespaces: bool = False,
        label_selector: str | None = None,
    ) -> list[dict[str, Any]]:
        """List resources of a given kind. Cluster-scoped kinds (Node, Namespace,
        ClusterRole, etc.) ignore namespace. Use all_namespaces=True for namespaced
        kinds across the cluster. label_selector is a standard '-l' string
        ('app=foo,role=bar')."""
        cmd = ["kubectl", "get", kind, "-o", "json"]
        if all_namespaces:
            cmd.append("--all-namespaces")
        elif namespace:
            cmd += ["-n", namespace]
        if label_selector:
            cmd += ["-l", label_selector]
        body = _run(cmd, parse_json=True)
        out: list[dict[str, Any]] = []
        for item in body.get("items", []):
            md = item.get("metadata", {})
            out.append(
                {
                    "name": md.get("name"),
                    "namespace": md.get("namespace"),
                    "kind": item.get("kind"),
                    "apiVersion": item.get("apiVersion"),
                    "labels": md.get("labels") or {},
                    "created": md.get("creationTimestamp"),
                }
            )
        return out

    @mcp.tool()
    def get_resource(kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
        """Return the full JSON for a single resource."""
        cmd = ["kubectl", "get", kind, name, "-o", "json"]
        if namespace:
            cmd += ["-n", namespace]
        return _run(cmd, parse_json=True)

    @mcp.tool()
    def describe_resource(kind: str, name: str, namespace: str | None = None) -> str:
        """Run `kubectl describe`. Useful when you want events + computed fields
        rather than the raw spec — e.g. to see why a pod is Pending."""
        cmd = ["kubectl", "describe", kind, name]
        if namespace:
            cmd += ["-n", namespace]
        return _run(cmd)

    @mcp.tool()
    def get_pod_logs(
        name: str,
        namespace: str,
        container: str | None = None,
        tail_lines: int = 200,
        previous: bool = False,
    ) -> str:
        """Read pod logs. previous=True reads the previous container instance
        (useful when the current one is in CrashLoopBackOff). tail_lines caps
        output to keep responses tractable."""
        cmd = [
            "kubectl",
            "logs",
            name,
            "-n",
            namespace,
            f"--tail={tail_lines}",
        ]
        if container:
            cmd += ["-c", container]
        if previous:
            cmd.append("-p")
        return _run(cmd)

    @mcp.tool()
    def list_events(
        namespace: str | None = None,
        all_namespaces: bool = False,
        field_selector: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent events. field_selector example: 'involvedObject.name=session-foo'."""
        cmd = ["kubectl", "get", "events", "-o", "json", "--sort-by=.lastTimestamp"]
        if all_namespaces:
            cmd.append("--all-namespaces")
        elif namespace:
            cmd += ["-n", namespace]
        if field_selector:
            cmd += ["--field-selector", field_selector]
        body = _run(cmd, parse_json=True)
        return [
            {
                "namespace": e.get("metadata", {}).get("namespace"),
                "type": e.get("type"),
                "reason": e.get("reason"),
                "message": e.get("message"),
                "involved": {
                    "kind": e.get("involvedObject", {}).get("kind"),
                    "name": e.get("involvedObject", {}).get("name"),
                },
                "count": e.get("count"),
                "lastTimestamp": e.get("lastTimestamp"),
            }
            for e in body.get("items", [])
        ]

    @mcp.tool()
    def top_pods(namespace: str | None = None, all_namespaces: bool = False) -> str:
        """`kubectl top pods` — CPU/memory by pod. Requires metrics-server."""
        cmd = ["kubectl", "top", "pods"]
        if all_namespaces:
            cmd.append("--all-namespaces")
        elif namespace:
            cmd += ["-n", namespace]
        return _run(cmd)

    @mcp.tool()
    def top_nodes() -> str:
        """`kubectl top nodes` — CPU/memory by node. Requires metrics-server."""
        return _run(["kubectl", "top", "nodes"])

    @mcp.tool()
    def helm_list(namespace: str | None = None, all_namespaces: bool = True) -> list[dict[str, Any]]:
        """List Helm releases. Defaults to all namespaces — narrow with namespace
        when looking for one specific release."""
        cmd = ["helm", "list", "-o", "json"]
        if all_namespaces and not namespace:
            cmd.append("-A")
        elif namespace:
            cmd += ["-n", namespace]
        return _run(cmd, parse_json=True)

    @mcp.tool()
    def helm_get_values(release: str, namespace: str, all_values: bool = True) -> dict[str, Any]:
        """Return a release's values. all_values=True merges chart defaults with
        user overrides; False returns only the user overrides."""
        cmd = ["helm", "get", "values", release, "-n", namespace, "-o", "json"]
        if all_values:
            cmd.append("-a")
        return _run(cmd, parse_json=True) or {}

    @mcp.tool()
    def helm_get_manifest(release: str, namespace: str) -> str:
        """Return the rendered manifest YAML for a release. Big — use sparingly."""
        return _run(["helm", "get", "manifest", release, "-n", namespace])

    @mcp.tool()
    def helm_status(release: str, namespace: str) -> dict[str, Any]:
        """Return release status (revision, deployed time, last action)."""
        return _run(
            ["helm", "status", release, "-n", namespace, "-o", "json"],
            parse_json=True,
        )

    @mcp.tool()
    def helm_history(release: str, namespace: str) -> list[dict[str, Any]]:
        """Return release revision history."""
        return _run(
            ["helm", "history", release, "-n", namespace, "-o", "json"],
            parse_json=True,
        )

    @mcp.tool()
    def delete_pod(name: str, namespace: str, grace_period_seconds: int | None = None) -> str:
        """Delete a Pod. Useful when a controller pod is wedged but its parent
        StatefulSet/Deployment/DaemonSet is healthy — the parent will recreate
        the pod. grace_period_seconds=0 forces immediate delete (skips
        terminationGracePeriod)."""
        cmd = ["kubectl", "delete", "pod", name, "-n", namespace]
        if grace_period_seconds is not None:
            cmd += [f"--grace-period={int(grace_period_seconds)}"]
        return _run(cmd)

    @mcp.tool()
    def rollout_restart(kind: str, name: str, namespace: str) -> str:
        """Trigger a rolling restart of a Deployment, StatefulSet, or DaemonSet.
        Equivalent to `kubectl rollout restart`: patches the pod template's
        `kubectl.kubernetes.io/restartedAt` annotation so the controller
        schedules new pods. kind must be one of: deployment, statefulset,
        daemonset."""
        allowed = {"deployment", "statefulset", "daemonset"}
        canonical = kind.lower().rstrip("s")
        if canonical not in allowed:
            raise ValueError(f"kind must be one of {sorted(allowed)}, got {kind!r}")
        return _run(["kubectl", "rollout", "restart", canonical, name, "-n", namespace])

    @mcp.tool()
    def api_resources() -> list[dict[str, Any]]:
        """List API resources known to the cluster — useful for discovering CRDs
        like applications.argoproj.io or httproutes.gateway.networking.k8s.io."""
        # `kubectl api-resources` doesn't have a -o json mode; parse the
        # default columnar output. Skip the header row.
        out = _run(
            [
                "kubectl",
                "api-resources",
                "--no-headers",
                "-o",
                "wide",
            ]
        )
        rows: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            # Layout: NAME [SHORTNAMES] APIVERSION NAMESPACED KIND VERBS...
            # SHORTNAMES is optional; detect by checking column widths via
            # the trailing fields, which are always [namespaced, kind, verbs...].
            name = parts[0]
            namespaced = parts[-3].lower() == "true"
            kind = parts[-2]
            verbs_field = parts[-1]
            api_version = parts[-4]
            shortnames = parts[1:-4] if len(parts) > 5 else []
            rows.append(
                {
                    "name": name,
                    "shortnames": shortnames,
                    "apiVersion": api_version,
                    "namespaced": namespaced,
                    "kind": kind,
                    "verbs": verbs_field.split(","),
                }
            )
        return rows
