#!/bin/sh
# Entrypoint for claude-container session pods.
#
# Auth to in-cluster HTTP MCP servers is now handled by the
# mcp-auth-proxy sidecar (see claude-container/mcp-auth-proxy/), which
# reads the projected SA token fresh per request. claude reaches MCPs
# via 127.0.0.1 ports declared in /workspace/.mcp.json — no
# Authorization header for this entrypoint to seed.
#
# This script is a thin passthrough kept for users who exec into the
# image directly (kubectl exec, docker run, etc.) and expect a normal
# CMD chain. The orchestrator's bridge bypasses ENTRYPOINT entirely
# (sessions.py sets command: ["sleep", "infinity"]) and runs its own
# bootstrap shell via kubectl exec.
set -e

exec "$@"
