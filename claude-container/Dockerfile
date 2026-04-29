# azure-mcp + github-mcp + k8s-mcp run as in-cluster HTTP MCP servers
# (see k8s-mcp-azure/, k8s-mcp-github/, k8s-mcp-k8s/). The container
# reaches them via the mcp-auth-proxy sidecar on 127.0.0.1, which reads
# the projected SA token fresh per request and forwards as a Bearer
# token. No co-located MCP binaries needed in this image.
#
# Base switched from node:20-alpine to mcr.microsoft.com/playwright:noble.
# Why: the agent (ambience and friends) needs to take its own screenshots
# as evidence — paths.json + runner-side capture is too brittle for
# non-effect change types (menu / settings / refactor). The playwright
# image ships chromium + all system deps preinstalled; alpine + glibc-
# bound chromium would be a much harder build. Net image size goes from
# ~250MB to ~2GB, amortized across the AKS node cache.
FROM mcr.microsoft.com/playwright:v1.49.1-noble

ARG TARGETARCH=amd64
ARG KUBECTL_VERSION=v1.31.0
ARG TOFU_VERSION=1.9.0
ARG HELM_VERSION=v3.16.3
ARG YQ_VERSION=v4.44.3
ARG UV_VERSION=0.5.18
ARG GO_VERSION=1.26.2

# OS toolchain. Switched apk -> apt-get with the base swap. ripgrep + make
# round out the bash surface (rg replaces grep for repo search; make is
# needed for several of the consumed repos' dev flows). github-cli moves
# below since Ubuntu's apt repo for it is set up separately.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        less \
        make \
        python3 \
        python3-pip \
        ripgrep \
        tmux \
        unzip \
        vim \
    && rm -rf /var/lib/apt/lists/*

# gh CLI from GitHub's official Ubuntu repo (no apt package by default).
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=${TARGETARCH} signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

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
# mcp-servers, platform-mcp). Glibc x86_64 build now (alpine's musl
# variant was the prior pinning; noble is glibc).
RUN curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
        | tar -xz -C /tmp \
    && mv /tmp/uv-x86_64-unknown-linux-gnu/uv /usr/local/bin/uv \
    && mv /tmp/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/uvx \
    && rm -rf /tmp/uv-x86_64-unknown-linux-gnu \
    && chmod +x /usr/local/bin/uv /usr/local/bin/uvx

# Go — for projects whose dev-loop builds a Go server (ambience, fzt).
# Adds ~200MB; one-time pull cost amortized across all sessions.
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
        | tar -C /usr/local -xz
ENV PATH="/usr/local/go/bin:${PATH}"

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

# Bootstrap script the orchestrator exec's on every WS connect. Lives
# in the image so the kubectl-exec request URL stays small — the
# kube-apiserver rejects oversized exec command lines with HTTP 400.
COPY claude-container/tank-bootstrap.sh /opt/tank/bootstrap.sh
RUN chmod +x /opt/tank/bootstrap.sh

# Rename the playwright base's pwuser (uid 1000) to `node` so existing
# bootstrap scripts, chart values, and securityContext references that
# named user `node` keep working. Same uid:gid (1000:1000), so the
# runAsUser: 1000 securityContext on session pods + ambience agent Jobs
# is unaffected.
RUN if id -u pwuser >/dev/null 2>&1; then \
        groupmod -n node pwuser \
        && usermod -l node -d /home/node -m pwuser; \
    else \
        useradd -m -u 1000 -s /bin/bash node; \
    fi

# Hide tmux's status bar so the claude TUI gets the full terminal height.
# tmux is the WS-reconnect-survival wrapper for the claude session
# (see tank-bootstrap.sh); the status bar would steal one row.
RUN printf 'set -g status off\nset -g default-terminal "tmux-256color"\n' > /home/node/.tmux.conf \
    && chown node:node /home/node/.tmux.conf

# Run the agent as a non-root user so claude's safety check accepts
# bypassPermissions mode (it refuses to enable bypass when uid 0). Also
# defense-in-depth: a tool-use exploit can't escape to a privileged shell.
RUN mkdir -p /workspace && chown -R node:node /workspace

# Playwright browser cache (chromium + deps) is at /ms-playwright in the
# base image. Surface it as PLAYWRIGHT_BROWSERS_PATH so child
# `npx playwright` calls find the bundled browsers instead of trying to
# re-download under /home/node/.cache.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /workspace
USER node

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
