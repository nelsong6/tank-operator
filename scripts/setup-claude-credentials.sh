#!/usr/bin/env bash
# Stash a Claude Code subscription credentials JSON in Azure Key Vault so
# session pods can launch claude as a logged-in subscriber instead of paying
# per-token via an API key.
#
# How to produce the JSON:
#   1. In a Linux env (WSL works on Windows), `npm i -g @anthropic-ai/claude-code`.
#   2. Run `claude` and complete `/login` in a browser.
#   3. `cat ~/.claude/.credentials.json` — that's the blob this script wants.
#
# The pod's bootstrap writes the same JSON to /root/.claude/.credentials.json
# before launching claude, which makes the TUI behave as if you'd logged in
# inside the container. The CLI auto-refreshes the access_token at runtime;
# those refreshes stay in the pod's filesystem (not back in KV), so every
# fresh pod starts from the snapshot here. Re-run when sessions stop
# authenticating (refresh token expired or revoked).
#
# Usage: scripts/setup-claude-credentials.sh

set -euo pipefail

VAULT="${VAULT:-romaine-kv}"
KV_SECRET_NAME="claude-code-credentials"
ESO_NAMESPACE="${ESO_NAMESPACE:-tank-operator-sessions}"
ESO_NAME="${ESO_NAME:-github-app-creds}"

require() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: '$1' is required but not on PATH" >&2
    exit 1
  fi
}
require az
require kubectl

cat <<'INSTRUCTIONS'
Storing your Claude Code subscription credentials in Azure Key Vault.

Paste the contents of ~/.claude/.credentials.json below, then press Ctrl-D.
(Generate by running `claude` in WSL/Linux and completing /login.)
INSTRUCTIONS

echo
JSON="$(cat)"

if [[ -z "${JSON// }" ]]; then
  echo "error: empty input, aborting" >&2
  exit 1
fi

if ! echo "${JSON}" | python3 -c 'import json,sys; json.load(sys.stdin)' >/dev/null 2>&1; then
  echo "error: input is not valid JSON, aborting" >&2
  exit 1
fi

echo "→ Writing Key Vault secret ${VAULT}/${KV_SECRET_NAME}…"
az keyvault secret set \
  --vault-name "${VAULT}" \
  --name "${KV_SECRET_NAME}" \
  --value "${JSON}" \
  --output none

echo "→ Forcing ExternalSecret refresh on ${ESO_NAMESPACE}/${ESO_NAME}…"
kubectl -n "${ESO_NAMESPACE}" annotate externalsecret "${ESO_NAME}" \
  "force-sync=$(date +%s)" --overwrite >/dev/null

cat <<'DONE'

✓ Credentials stored. New "subscription" sessions will boot logged-in.

Note: pods already running will NOT see the new value. Kill + recreate.
DONE
