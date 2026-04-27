#!/bin/sh
# Entrypoint for claude-container session pods.
#
# Exports MCP_*_BEARER env vars from the projected K8s ServiceAccount
# token so mcp.json's HTTP MCP entries can authenticate to in-cluster
# MCP servers (kube-rbac-proxy validates these via TokenReview, then
# forwards to the upstream MCP binary running with its own UAMI).
#
# Same token works for every MCP server — kube-rbac-proxy's
# SubjectAccessReview gate is what differentiates which servers each
# SA can invoke. Per-server env vars exist so future audience-bound
# tokens (kubectl projected with --audience=mcp-azure) can scope
# differently if we tighten later.
#
# Tokens rotate on disk (~50min eager-renewal) but env vars don't
# update once exported — sessions exceeding that interval would 401.
# Acceptable for tank-operator's ephemeral-session model; if it
# starts mattering, swap for a sidecar that injects fresh tokens.
set -e

TOKEN_PATH=/var/run/secrets/kubernetes.io/serviceaccount/token
if [ -r "$TOKEN_PATH" ]; then
    TOKEN="$(cat $TOKEN_PATH)"
    export MCP_AZURE_BEARER="$TOKEN"
    export MCP_GITHUB_BEARER="$TOKEN"
fi

exec "$@"
