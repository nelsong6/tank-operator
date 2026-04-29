# Custom ARC runner image for tank-operator's issue-agent flow. Bakes in
# the tools the workflow needs (az + kubectl) so each run doesn't pay the
# per-job install cost. Same shape as ambience/.github/runner/Dockerfile.
#
# Built + pushed by .github/workflows/issue-agent-runner-build.yml; the
# tag in k8s-arc-runners/values.yaml is rewritten there on each build.
FROM ghcr.io/actions/actions-runner:latest

USER root

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        sudo \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Runner user needs passwordless sudo for any in-job step that elevates.
# `actions/actions-runner` already creates the user; this just confirms
# the policy.
RUN echo "runner ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner

# Azure CLI — needed by `azure/login@v2` and `az aks get-credentials`.
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# kubectl — needed by every step that talks to AKS. Pinned to match the
# cluster's minor version (the cluster runs 1.31 per claude-container's
# KUBECTL_VERSION).
RUN KUBECTL_VERSION=v1.31.0 \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

USER runner
