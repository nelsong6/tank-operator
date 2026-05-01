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
#                                   in remote_control mode, remoteDialogSeen
#                                   so the `/remote-control` slash command
#                                   skips its first-run consent prompt
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
cat > $HOME/.claude/settings.json <<'EOF'
{"theme":"dark","permissions":{"defaultMode":"bypassPermissions"},"skipDangerousModePermissionPrompt":true}
EOF
mcp_enabled='[]'
if [ -f /workspace/.mcp.json ]; then
  mcp_enabled="$(jq -c '.mcpServers | keys' /workspace/.mcp.json)"
fi
case "${TANK_SESSION_MODE:-api_key}" in
  subscription|remote_control)
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
# Remote-control mode needs `remoteDialogSeen` in ~/.claude.json to
# suppress the one-time interactive
#   "Enable Remote Control? (y/n)"
# consent prompt that would otherwise block reading from stdin when the
# bootstrap auto-runs `claude '/remote-control'`.
#
# Earlier (cf57df6) we also wrote a placeholder `oauthAccount` with fake
# UUIDs to satisfy `claude remote-control`'s (bridge mode) startup
# eligibility check. That placeholder is GONE now: the slash-command
# path runs its eligibility check synchronously when /remote-control is
# invoked, and the fake UUIDs caused the command to refuse to launch
# with "/remote-control isn't available in this environment". Real
# OAuth bytes flow through the api-proxy's injection, so eligibility
# resolves against the actor's real org without any local placeholder.
remote_dialog_block=''
if [ "${TANK_SESSION_MODE}" = "remote_control" ]; then
  remote_dialog_block='"remoteDialogSeen": true,'
fi
cat > $HOME/.claude.json <<EOF
{
  "hasCompletedOnboarding": true,
  ${api_key_block}
  ${remote_dialog_block}
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

if [ "${TANK_SESSION_MODE}" = "remote_control" ]; then
  # Same shape as subscription mode (one tmux session, claude in the
  # foreground), but launch with the `/remote-control` slash command
  # pre-typed so the bridge URL prints in the TUI on session start.
  # The user clicks the URL in the TUI to continue from claude.ai/code;
  # both surfaces share one conversation.
  #
  # Why not `claude remote-control` (the bridge-only mode)? The
  # claude.ai/code UI's URL-arrival flow is broken upstream
  # (anthropics/claude-code#34581 family) — multi-turn fails after the
  # first reply when the worker is a bare bridge. The slash-command path
  # attaches a real Claude Code TUI as the worker, which the URL-arrival
  # flow handles correctly.
  exec tmux new-session -s tank "claude '/remote-control'; exec bash"
fi

exec tmux new-session -s tank 'claude; exec bash'
