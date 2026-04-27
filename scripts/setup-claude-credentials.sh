#!/usr/bin/env bash
# Seed the Claude Code subscription credentials in Azure Key Vault. From there
# ExternalSecret pulls them once into a K8s Secret in the orchestrator
# namespace; the in-cluster OAuth gateway (POST /v1/oauth/token in
# tank-operator's FastAPI app) is the only thing that reads or writes that
# Secret going forward. Session pods never see the refresh token.
#
# How to produce the JSON:
#   1. In a Linux env (WSL works on Windows), `npm i -g @anthropic-ai/claude-code`.
#   2. Run `claude` and complete `/login` in a browser.
#   3. `cat ~/.claude/.credentials.json` — that's the blob this script wants.
#
# When to re-run: only when the refresh chain dies entirely (e.g. you revoked
# access manually, or the gateway has been off long enough that the refresh
# token expired). In normal operation the gateway rotates the refresh token
# in the K8s Secret on every refresh, so KV drifts intentionally — KV is the
# disaster-recovery seed, not the live source of truth.
#
# Usage: scripts/setup-claude-credentials.sh

set -euo pipefail

VAULT="${VAULT:-romaine-kv}"
KV_SECRET_NAME="claude-code-credentials"
# After re-seeding KV we force-sync the ExternalSecret that lives in the
# orchestrator namespace (refreshInterval: 0s, so without an explicit poke
# ESO never re-reads from KV).
ESO_NAMESPACE="${ESO_NAMESPACE:-tank-operator}"
ESO_NAME="${ESO_NAME:-claude-code-credentials}"

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

✓ Credentials seeded. The OAuth gateway will pick up the new refresh token
on its next call to platform.claude.com.

Note: the orchestrator pod caches the refresh token in memory after first
read; restart it (kubectl -n tank-operator rollout restart deploy/tank-operator)
to force an immediate re-read. Existing session pods are unaffected — they
talk to the gateway, not to KV.
DONE
