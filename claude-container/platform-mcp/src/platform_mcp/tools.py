import os
import re
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


REPO_ROOT_ENV = "PLATFORM_MCP_REPO_ROOT"
DEFAULT_REPO_ROOT = "/workspace"

_ACTION_LINE = re.compile(r"^\s*#\s+(?P<address>.+?)\s+will be (?P<action>.+?)\s*$")
_PLAN_LINE = re.compile(
    r"^Plan:\s+(?P<add>\d+)\s+to add,\s+(?P<change>\d+)\s+to change,\s+(?P<destroy>\d+)\s+to destroy"
)


def _repo_root() -> Path:
    return Path(os.environ.get(REPO_ROOT_ENV, DEFAULT_REPO_ROOT))


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def tofu_plan_summary(directory: str = "tofu") -> dict[str, Any]:
        """Run `tofu plan` in <repo>/<directory> and return a structured summary.

        Returns: { exit_code, add, change, destroy, resources: [{address, action}], stderr_tail, stdout_tail }.
        Resource addresses come from the `# X will be Y` headers tofu emits before each diff block;
        counts come from the trailing `Plan: A to add, B to change, C to destroy.` line.
        """
        cwd = _repo_root() / directory
        try:
            result = subprocess.run(
                ["tofu", "plan", "-no-color", "-input=false"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as e:
            return {"error": f"tofu binary not found: {e}", "exit_code": None}

        return _parse_plan(result.stdout, result.stderr, result.returncode)


def _parse_plan(stdout: str, stderr: str, exit_code: int) -> dict[str, Any]:
    resources: list[dict[str, str]] = []
    add = change = destroy = 0
    for line in stdout.splitlines():
        m = _ACTION_LINE.match(line)
        if m:
            resources.append({"address": m["address"], "action": m["action"]})
            continue
        m = _PLAN_LINE.match(line)
        if m:
            add, change, destroy = int(m["add"]), int(m["change"]), int(m["destroy"])
    return {
        "exit_code": exit_code,
        "add": add,
        "change": change,
        "destroy": destroy,
        "resources": resources,
        "stdout_tail": "\n".join(stdout.splitlines()[-40:]),
        "stderr_tail": "\n".join(stderr.splitlines()[-40:]) if stderr else "",
    }
