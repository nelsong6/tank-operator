#!/usr/bin/env bash
# Stash a Claude Code subscription OAuth token in Azure Key Vault so session
# pods can use the user's claude.ai subscription instead of an API key.
#
# Run this once per token rotation (the token is good for ~1 year). It
# refreshes the ExternalSecret immediately so newly-created sessions pick
# up the new value without waiting for the 1h ESO poll.
#
# Usage: scripts/setup-claude-token.sh

set -euo pipefail

VAULT="${VAULT:-romaine-kv}"
KV_SECRET_NAME="claude-code-oauth-token"
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
Generating a long-lived OAuth token tied to your claude.ai subscription.

In another terminal (or below), run:
    claude setup-token

It will open a browser, you'll authenticate against claude.ai, and the CLI
will print a token (starts with `sk-ant-oat-...`). Copy it and paste it here.
INSTRUCTIONS

echo
read -rsp "Paste token: " TOKEN
echo

if [[ -z "${TOKEN}" ]]; then
  echo "error: empty token, aborting" >&2
  exit 1
fi

echo "→ Writing Key Vault secret ${VAULT}/${KV_SECRET_NAME}…"
az keyvault secret set \
  --vault-name "${VAULT}" \
  --name "${KV_SECRET_NAME}" \
  --value "${TOKEN}" \
  --output none

echo "→ Forcing ExternalSecret refresh on ${ESO_NAMESPACE}/${ESO_NAME}…"
kubectl -n "${ESO_NAMESPACE}" annotate externalsecret "${ESO_NAME}" \
  "force-sync=$(date +%s)" --overwrite >/dev/null

cat <<'DONE'

✓ Token stored. Newly created sessions will see CLAUDE_CODE_OAUTH_TOKEN.

Note: pods that are already running will NOT pick up the new value (the env
var is captured at pod creation). Click the 'x' on the session tile to kill
it, then '+ new' for a fresh one.
DONE
