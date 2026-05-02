#!/bin/bash
# Session-pod bootstrap, exec'd by the tank-operator orchestrator over
# the kubectl-exec WebSocket. Reads pod env (TANK_SESSION_MODE,
# ANTHROPIC_API_KEY) and seeds claude state so a fresh pod boots
# straight to the chat prompt.
#
# Lives in the image (not inlined in the orchestrator's exec args)
# because the kube-apiserver rejects oversized exec request URLs:
# every byte of the exec command is URL-encoded into ?command=... and
# the bootstrap had grown past the apiserver's request-line limit,
# causing reconnects to 400 with WSServerHandshakeError.
#
# State seeded here:
#   ~/.claude/CLAUDE.md           — default global primer copied from the
#                                   image (claude-container/default-claude.md).
#                                   User-scope context loaded into every prompt.
#   ~/.claude/settings.json       — theme + bypassPermissions defaultMode +
#                                   skipDangerousModePermissionPrompt
#   ~/.claude.json                — onboarding flag + API-key trust list
#                                   (claude keys off the last 20 chars; we
#                                   include 22 too in case that flips back) +
#                                   per-project trust for /workspace +
#                                   official-marketplace auto-install flags +
#                                   pre-approved set of project-level MCP
#                                   servers (read from /workspace/.mcp.json so
#                                   it stays correct as the image evolves) +
#                                   remoteDialogSeen so the `/remote-control`
#                                   slash command skips its first-run consent
#                                   prompt when the user clicks the frontend's
#                                   "Remote control" button
#   ~/.claude/.credentials.json   — only in subscription mode: a static
#                                   placeholder blob. The real token is
#                                   never written to the pod. The
#                                   in-cluster api-proxy strips claude's
#                                   Authorization on every request and
#                                   injects the current real Bearer.
#   ~/.claude/skills/<name>/      — SKILL.md files pulled from external
#                                   repos via /opt/tank/fetch-skills.py
#                                   (uses the github MCP for auth; soft
#                                   fails so a transient MCP error does
#                                   not block boot).
#
# claude runs inside a named tmux session ("tank") so reconnects re-attach
# the same PTY/scrollback. If claude exits we fall through to bash so the
# WS stays useful.

# Reconnect fast-path: if the tmux session already exists this is a
# reattach, not a fresh boot. Skip settings/credentials setup (already
# done on first connect; rewriting is idempotent but wasteful, and in
# subscription mode would re-hit the OAuth gateway every reconnect).
if tmux has-session -t tank 2>/dev/null; then
  exec tmux attach-session -t tank
fi
# Config-mode: short-circuit the regular session bootstrap. The user is
# here to do `claude /login` once so we can capture credentials.json and
# write it to KV. No MCP wiring, no onboarding bypass, no credentials
# pre-seed — claude needs to see a clean state to walk through OAuth.
# The orchestrator's POST /api/sessions/{id}/save-credentials reads the
# resulting ~/.claude/.credentials.json out of this pod via exec.
if [ "${TANK_SESSION_MODE}" = "config" ]; then
  mkdir -p $HOME/.claude
  cat > $HOME/.claude/settings.json <<'EOF'
{"theme":"dark"}
EOF
  cat > $HOME/.claude.json <<'EOF'
{"hasCompletedOnboarding": true}
EOF
  exec claude /login
fi
# MCP auth is delegated to the mcp-auth-proxy sidecar — claude reaches
# in-cluster HTTP MCP servers via 127.0.0.1 ports declared in
# /workspace/.mcp.json, and the sidecar reads the projected SA token
# fresh per request. No bearer-env-var wiring needed here anymore.
mkdir -p $HOME/.claude
cp /opt/claude-container/CLAUDE.md $HOME/.claude/CLAUDE.md
cat > $HOME/.claude/settings.json <<'EOF'
{"theme":"dark","permissions":{"defaultMode":"bypassPermissions"},"skipDangerousModePermissionPrompt":true}
EOF
mcp_enabled='[]'
if [ -f /workspace/.mcp.json ]; then
  mcp_enabled="$(jq -c '.mcpServers | keys' /workspace/.mcp.json)"
fi
case "${TANK_SESSION_MODE:-api_key}" in
  subscription)
    # Static placeholder credentials. The api-proxy in front of
    # api.anthropic.com strips this Authorization on every request and
    # injects the real token, so claude never needs valid creds locally.
    # expiresAt is set to year 2286 so claude never decides to refresh
    # on its own; the placeholder refreshToken would 400 immediately at
    # platform.claude.com if it ever did.
    creds_path=$HOME/.claude/.credentials.json
    cat > "$creds_path" <<'EOF'
{
  "claudeAiOauth": {
    "accessToken": "managed-by-tank-operator",
    "refreshToken": "managed-by-tank-operator",
    "expiresAt": 9999999999000,
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "max",
    "rateLimitTier": "max"
  }
}
EOF
    chmod 600 "$creds_path"
    unset ANTHROPIC_API_KEY
    api_key_block=''
    ;;
  *)
    last20="${ANTHROPIC_API_KEY: -20}"
    last22="${ANTHROPIC_API_KEY: -22}"
    api_key_block="\"customApiKeyResponses\": {\"approved\": [\"${last20}\", \"${last22}\"], \"rejected\": []},"
    ;;
esac
# `remoteDialogSeen` skips the one-time interactive
#   "Enable Remote Control? (y/n)"
# consent prompt the first time `/remote-control` runs in a session.
# Set unconditionally because the frontend's "Remote control" button
# can fire the slash command on any subscription session, and the
# consent prompt would block stdin and break the flow.
#
# Earlier (cf57df6) we also wrote a placeholder `oauthAccount` with fake
# UUIDs to satisfy `claude remote-control`'s (bridge mode) startup
# eligibility check. That placeholder is GONE: the slash-command path
# runs its eligibility check against the actor's real org (resolved by
# the api-proxy's OAuth injection), and the fake UUIDs caused the
# command to refuse to launch with "/remote-control isn't available in
# this environment". The whole `remote_control` session mode was
# removed in favor of an in-TUI button — see frontend/src/App.tsx.
cat > $HOME/.claude.json <<EOF
{
  "hasCompletedOnboarding": true,
  ${api_key_block}
  "remoteDialogSeen": true,
  "officialMarketplaceAutoInstallAttempted": true,
  "officialMarketplaceAutoInstalled": true,
  "projects": {
    "/workspace": {
      "allowedTools": [],
      "mcpContextUris": [],
      "mcpServers": {},
      "enabledMcpjsonServers": ${mcp_enabled},
      "disabledMcpjsonServers": [],
      "hasTrustDialogAccepted": true,
      "projectOnboardingSeenCount": 1,
      "hasClaudeMdExternalIncludesApproved": false,
      "hasClaudeMdExternalIncludesWarningShown": false,
      "lastGracefulShutdown": false
    }
  }
}
EOF
# Pull SKILL.md files from external repos via the github MCP. Soft fail
# — a transient MCP error logs `[skills]` lines but does not block boot.
if [ -x /opt/tank/fetch-skills.py ]; then
  python3 /opt/tank/fetch-skills.py 2>&1 | sed 's/^/[skills] /' || true
fi

exec tmux new-session -s tank 'claude; exec bash'
