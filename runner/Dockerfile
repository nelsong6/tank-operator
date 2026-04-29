# Custom ARC runner image for tank-operator's issue-agent flow.
# Bakes in `kubectl` so the workflow doesn't pay a per-job install.
# No `az` — the runner talks to the cluster as itself with its own SA
# (k8s-arc-runners/templates/runner-rbac.yaml grants the perms),
# not via Azure ARM + AKS get-credentials.
#
# Built + pushed by .github/workflows/issue-agent-runner-build.yml;
# the tag in k8s-arc-runners/values.yaml is rewritten there on each
# build.
FROM ghcr.io/actions/actions-runner:latest

USER root

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gettext-base \
        git \
        jq \
    && rm -rf /var/lib/apt/lists/*

# kubectl, pinned to the cluster's minor version (1.31, matches
# claude-container's KUBECTL_VERSION).
RUN KUBECTL_VERSION=v1.31.0 \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

USER runner
