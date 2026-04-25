#!/usr/bin/env bash
# Stash a claude.ai subscription credentials blob in Azure Key Vault so session
# pods can launch the claude TUI fully authenticated against the user's
# subscription (no API-credit burn).
#
# We use the full ~/.claude/.credentials.json (with `user:sessions:claude_code`
# scope) rather than ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN: API keys
# bill API credits, and the env-var OAuth path is "inference-only" — only the
# credentials file satisfies the interactive TUI.
#
# To get the JSON: in any Linux/WSL shell where claude is installed, run
# `claude /login`, complete the browser flow, then `cat ~/.claude/.credentials.json`.
# Paste the entire blob (single line) into this script.
#
# Run on rotation. The script force-syncs the ExternalSecret so new session
# pods pick up the value immediately (no waiting on the 1h ESO poll).
#
# Usage: scripts/setup-claude-credentials.sh

set -euo pipefail

VAULT="${VAULT:-romaine-kv}"
KV_SECRET_NAME="claude-credentials-json"
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
require jq

cat <<'INSTRUCTIONS'
Storing your claude.ai subscription credentials in Azure Key Vault.

In a Linux/WSL shell where claude is installed:
    claude /login          # complete the browser flow
    cat ~/.claude/.credentials.json

Paste the entire JSON blob below — input is hidden.
INSTRUCTIONS

echo
read -rsp "Paste credentials JSON: " BLOB
echo

if [[ -z "${BLOB}" ]]; then
  echo "error: empty value, aborting" >&2
  exit 1
fi
if ! printf '%s' "${BLOB}" | jq -e '.claudeAiOauth.refreshToken' >/dev/null 2>&1; then
  echo "error: input doesn't look like a credentials.json (missing .claudeAiOauth.refreshToken)" >&2
  exit 1
fi

echo "→ Writing Key Vault secret ${VAULT}/${KV_SECRET_NAME}…"
az keyvault secret set \
  --vault-name "${VAULT}" \
  --name "${KV_SECRET_NAME}" \
  --value "${BLOB}" \
  --output none

echo "→ Forcing ExternalSecret refresh on ${ESO_NAMESPACE}/${ESO_NAME}…"
kubectl -n "${ESO_NAMESPACE}" annotate externalsecret "${ESO_NAME}" \
  "force-sync=$(date +%s)" --overwrite >/dev/null

cat <<'DONE'

✓ Credentials stored. Newly created sessions will see CLAUDE_CREDENTIALS_JSON
  in their env, and the bootstrap drops it at /root/.claude/.credentials.json.

Note: pods that are already running will NOT pick up the new value (env vars
are captured at pod creation). Click the 'x' on the session tile to kill it,
then '+ new' for a fresh one.
DONE
