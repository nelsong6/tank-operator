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

RUN apk add --no-cache \
        bash \
        ca-certificates \
        curl \
        git \
        github-cli \
        gnupg \
        jq \
        less \
        py3-pip \
        python3 \
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

RUN npm install -g @anthropic-ai/claude-code

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

# Thin passthrough entrypoint kept for direct `docker run` / `kubectl
# exec` consumers. The orchestrator's bridge bypasses ENTRYPOINT (sets
# command: ["sleep", "infinity"]) and runs its own bootstrap shell.
# In-cluster MCP auth is handled by the mcp-auth-proxy sidecar above.
COPY claude-container/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Hide tmux's status bar so the claude TUI gets the full terminal height.
# tmux is the WS-reconnect-survival wrapper for the claude session
# (see exec_proxy._BOOTSTRAP_SH); the status bar would steal one row.
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
