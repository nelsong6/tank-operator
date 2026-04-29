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
# Pinned to 2.71.0 (April 2025) because the bleeding-edge stream defaults
# to api-version `2026-01-01` for AKS operations, which the cluster's
# resource provider doesn't yet recognise (max supported is `2025-04-01`).
# Downgrade once Azure rolls forward the AKS api-version cluster-side.
RUN apt-get update \
    && apt-get install -y --no-install-recommends lsb-release \
    && mkdir -p /etc/apt/keyrings \
    && curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
    && AZ_REPO=$(lsb_release -cs) \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ ${AZ_REPO} main" \
        > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "azure-cli=2.71.0-1~${AZ_REPO}" \
    && rm -rf /var/lib/apt/lists/*

# kubectl — needed by every step that talks to AKS. Pinned to match the
# cluster's minor version (the cluster runs 1.31 per claude-container's
# KUBECTL_VERSION).
RUN KUBECTL_VERSION=v1.31.0 \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

USER runner
