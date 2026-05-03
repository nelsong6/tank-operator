# azure-mcp + github-mcp + k8s-mcp run as in-cluster HTTP MCP servers
# (see k8s-mcp-azure/, k8s-mcp-github/, k8s-mcp-k8s/). The container
# reaches them via the mcp-auth-proxy sidecar on 127.0.0.1, which reads
# the projected SA token fresh per request and forwards as a Bearer
# token. No co-located MCP binaries needed in this image — keep it
# minimal.
FROM node:20-alpine

ARG TARGETARCH=amd64
ARG KUBECTL_VERSION=v1.31.0
ARG TOFU_VERSION=1.9.0
ARG HELM_VERSION=v3.16.3
ARG YQ_VERSION=v4.44.3
ARG UV_VERSION=0.5.18

# ripgrep + make round out the bash surface (rg replaces grep for repo
# search; make is needed for several of the consumed repos' dev flows).
RUN apk add --no-cache \
        bash \
        ca-certificates \
        curl \
        git \
        github-cli \
        gnupg \
        jq \
        less \
        make \
        py3-pip \
        python3 \
        ripgrep \
        tmux \
        unzip \
        vim

RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

RUN curl -fsSL "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_${TARGETARCH}.zip" \
        -o /tmp/tofu.zip \
    && unzip -q /tmp/tofu.zip tofu -d /usr/local/bin \
    && chmod +x /usr/local/bin/tofu \
    && rm /tmp/tofu.zip

# helm — every consumed repo ships a chart; without it `helm template` /
# `helm get manifest` debugging is missing.
RUN curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
        | tar -xz -C /tmp \
    && mv "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm \
    && rm -rf "/tmp/linux-${TARGETARCH}" \
    && chmod +x /usr/local/bin/helm

# yq — pairs with jq for the YAML half of k8s/helm work.
RUN curl -fsSL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${TARGETARCH}" \
        -o /usr/local/bin/yq \
    && chmod +x /usr/local/bin/yq

# uv — fast pip replacement for the python projects (glimmung,
# mcp-servers, platform-mcp). Hardcoded musl x86_64 path because
# astral-sh/uv release artifacts aren't named with docker's TARGETARCH
# convention; the image is alpine + amd64 today.
RUN curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-musl.tar.gz" \
        | tar -xz -C /tmp \
    && mv /tmp/uv-x86_64-unknown-linux-musl/uv /usr/local/bin/uv \
    && mv /tmp/uv-x86_64-unknown-linux-musl/uvx /usr/local/bin/uvx \
    && rm -rf /tmp/uv-x86_64-unknown-linux-musl \
    && chmod +x /usr/local/bin/uv /usr/local/bin/uvx

RUN npm install -g @anthropic-ai/claude-code @openai/codex

COPY claude-container/platform-mcp /opt/platform-mcp
RUN pip install --no-cache-dir --break-system-packages /opt/platform-mcp

# Localhost reverse proxy that injects fresh SA-token bearer auth into
# outbound HTTP MCP calls. Runs as a sidecar container in the session
# pod (same image, command "mcp-auth-proxy"). See server.py for the
# full rationale — TL;DR fixes the 1h SA-token expiry that froze the
# bearer in env vars.
COPY claude-container/mcp-auth-proxy /opt/mcp-auth-proxy
RUN pip install --no-cache-dir --break-system-packages /opt/mcp-auth-proxy

# Default MCP wiring for the in-container agent. Lives in /workspace so
# Claude Code picks it up as project-scope config; also kept at
# /opt/claude-container/mcp.json for users who mount their own /workspace.
COPY claude-container/mcp.json /opt/claude-container/mcp.json
RUN mkdir -p /workspace && cp /opt/claude-container/mcp.json /workspace/.mcp.json

# Default project-scope CLAUDE.md primer. Lands in /workspace so Claude
# Code auto-loads it for any cwd under /workspace (including cloned
# repos), describing the pod environment without conflating with
# user-scope (~/.claude/CLAUDE.md), which is left free for genuine user
# prefs. Also kept at /opt/claude-container/CLAUDE.md for users who
# mount their own /workspace. Source filename is intentionally not
# CLAUDE.md so Claude Code won't auto-load the seed as project docs
# when an agent works in claude-container/ inside the tank-operator repo.
COPY claude-container/default-claude.md /opt/claude-container/CLAUDE.md
RUN cp /opt/claude-container/CLAUDE.md /workspace/CLAUDE.md

# Thin passthrough entrypoint kept for direct `docker run` / `kubectl
# exec` consumers. The orchestrator's bridge bypasses ENTRYPOINT (sets
# command: ["sleep", "infinity"]) and runs its own bootstrap shell.
# In-cluster MCP auth is handled by the mcp-auth-proxy sidecar above.
COPY claude-container/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Bootstrap script the orchestrator exec's on every WS connect. Lives
# in the image so the kubectl-exec request URL stays small — the
# kube-apiserver rejects oversized exec command lines with HTTP 400.
COPY claude-container/tank-bootstrap.sh /opt/tank/bootstrap.sh
RUN chmod +x /opt/tank/bootstrap.sh

# Skill-sync helper: tank-bootstrap.sh runs this on every session boot
# to pull SKILL.md files from external repos via the github MCP.
COPY claude-container/fetch-skills.py /opt/tank/fetch-skills.py
RUN chmod +x /opt/tank/fetch-skills.py

# Hide tmux's status bar so the claude TUI gets the full terminal height.
# tmux is the WS-reconnect-survival wrapper for the claude session
# (see tank-bootstrap.sh); the status bar would steal one row.
RUN printf 'set -g status off\nset -g default-terminal "tmux-256color"\n' > /home/node/.tmux.conf \
    && chown node:node /home/node/.tmux.conf

# Run the agent as a non-root user so claude's safety check accepts
# bypassPermissions mode (it refuses to enable bypass when uid 0). Also
# defense-in-depth: a tool-use exploit can't escape to a privileged shell.
# node:20-alpine already ships a `node` user at uid/gid 1000 with
# /home/node, so reuse it (creating another user with gid 1000 collides).
RUN chown -R node:node /workspace

WORKDIR /workspace
USER node

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
