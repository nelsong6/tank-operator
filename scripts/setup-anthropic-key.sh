#!/usr/bin/env bash
# Stash an Anthropic API key in Azure Key Vault so session pods can launch
# claude CLI fully authenticated (TUI + all features).
#
# We use ANTHROPIC_API_KEY rather than the OAuth subscription path
# (CLAUDE_CODE_OAUTH_TOKEN) because the env-var token is "inference-only"
# and the full OAuth flow assumes a browser on the same machine — neither
# fits a headless container.
#
# Run on rotation. The script force-syncs the ExternalSecret so new session
# pods pick up the value immediately (no waiting on the 1h ESO poll).
#
# Usage: scripts/setup-anthropic-key.sh

set -euo pipefail

VAULT="${VAULT:-romaine-kv}"
KV_SECRET_NAME="anthropic-api-key"
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
Storing your Anthropic API key in Azure Key Vault.

Get a key at https://console.anthropic.com/settings/keys (starts with `sk-ant-api...`).
Paste it below — input is hidden.
INSTRUCTIONS

echo
read -rsp "Paste API key: " TOKEN
echo

if [[ -z "${TOKEN}" ]]; then
  echo "error: empty value, aborting" >&2
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

✓ Key stored. Newly created sessions will see ANTHROPIC_API_KEY in their env.

Note: pods that are already running will NOT pick up the new value (env vars
are captured at pod creation). Click the 'x' on the session tile to kill it,
then '+ new' for a fresh one.
DONE
