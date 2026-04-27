# Migration: claude-container + MCP servers from infra-bootstrap

This branch moves the `claude-container` image, the `mcp-azure` / `mcp-github`
HTTP MCP servers, and their tofu (UAMIs, build SPs, federated creds, KV
secrets) out of `infra-bootstrap` and into `tank-operator`. The shared AKS
cluster, ACR, and Key Vault stay in infra-bootstrap and are read here as
data sources.

The changes in **this repo** (already on this branch):

- `claude-container/` — image source (Dockerfile, mcp.json, entrypoint.sh, platform-mcp/).
- `mcp-servers/github/` — Python source for the custom mcp-github image.
- `k8s-mcp-azure/`, `k8s-mcp-github/` — Helm charts.
- `infra/mcp.tf`, `infra/mcp-server/`, `infra/claude_container_ci.tf`, `infra/mcp_github_ci.tf` — UAMIs + build SPs.
- `infra/azure_data.tf` — data sources for the shared RG/ACR/AKS/KV (resources owned by infra-bootstrap).
- Updated `infra/providers.tf`, `infra/terraform.tf` — adds the `github` provider.
- Updated `infra/oauth_app.tf` — uses the consolidated `data.azurerm_key_vault.main` data source.
- New workflows: `.github/workflows/claude-container-build.yml`, `.github/workflows/mcp-github-build.yml`.
- Updated `.github/workflows/build.yml` and `tofu.yml` (path filters + GITHUB_TOKEN for the github provider).

## Apply order (do not skip steps; some are not reversible)

ArgoCD's `mcp-azure` / `mcp-github` Applications still live in `infra-bootstrap`
under `k8s/apps/` and currently point at `repoURL: nelsong6/infra-bootstrap`.
We flip them to point at this repo, then delete the chart sources from
infra-bootstrap.

The destroy/recreate strategy means the new SPs/UAMIs have **different
client IDs** than the old ones. The `mcp-*-mi-client-id` KV secret values
change; ESO syncs them; mcp-azure pod re-reads via `envFrom` on its next
Deployment rollout (force one with `kubectl rollout restart`). Brief window
where session pods may see 401s from azure-mcp until the rollout completes.

### 1. Land this branch

Merge this branch (or push direct, per your direct-to-main convention).
The image-build workflows are now present but won't push anything yet —
they need the new SPs (created by step 2).

### 2. Apply tofu in tank-operator (creates the new identities)

The `infra/` workflow runs on push to main and applies. It will:

- Create `azuread_application.claude_container_ci`, `mcp_github_ci`.
- Create `azuread_application_federated_identity_credential.*_ci_main` (subject `repo:nelsong6/tank-operator:ref:refs/heads/main`).
- Grant `AcrPush` on the shared ACR to both new SPs.
- Create UAMI `mcp-azure-identity` + federated cred to `system:serviceaccount:mcp-azure:mcp-azure`.
- Grant Reader on the subscription to that UAMI.
- Write `mcp-tenant-id` and `mcp-azure-mi-client-id` to KV.
- Write GitHub Actions vars `CLAUDE_CONTAINER_CI_CLIENT_ID`, `MCP_GITHUB_CI_CLIENT_ID` on `tank-operator`.

Old infra-bootstrap-owned counterparts (same names, different state file)
co-exist temporarily. Azure tolerates this.

**Likely failure mode**: if tank-operator's deployer SP (`vars.ARM_CLIENT_ID`)
lacks `Microsoft.Authorization/roleAssignments/write` on the ACR scope, the
two `azurerm_role_assignment.*_ci_acr_push` resources will 403. To fix
without changing tank-operator: in infra-bootstrap, grant the tank-operator
deployer SP `Owner` (or `User Access Administrator`) on the ACR scope. Or
move just those two role-assignment resources into infra-bootstrap.

### 3. Build the images on the new SPs

Push a no-op change touching `claude-container/` (and one touching
`mcp-servers/github/`) to trigger the new workflows on main. They use the
GH Actions vars written in step 2. Verify pushes land at
`romainecr.azurecr.io/claude-container:<sha>` and `mcp-github:<sha>`.

### 4. Flip ArgoCD Applications in infra-bootstrap

Edit `k8s/apps/mcp-azure.yaml` and `k8s/apps/mcp-github.yaml` in infra-bootstrap:

```yaml
spec:
  source:
    repoURL: https://github.com/nelsong6/tank-operator.git    # was: infra-bootstrap.git
    targetRevision: main
    path: k8s-mcp-azure                                       # was: k8s/mcp-azure
    # (and path: k8s-mcp-github for the github one)
```

Apply via your normal infra-bootstrap chart sync. ArgoCD will see no diff
because both repos contain the same chart at this point. (Self-heal might
flicker once during the source flip.)

### 5. Force the mcp-azure pod to pick up the new client ID

The UAMI client ID changed in step 2; ESO syncs the new value into the
`mcp-azure-config` Secret on its 1-hour refresh, but the pod's `envFrom`
only re-reads at startup. After step 4:

```bash
kubectl -n mcp-azure rollout restart deployment/mcp-azure
```

If you don't want to wait for ESO, force a sync:

```bash
kubectl -n mcp-azure annotate externalsecret/mcp-azure-config force-sync="$(date +%s)" --overwrite
```

mcp-github doesn't have this concern (no UAMI; its config comes from
pre-existing GitHub App KV secrets that didn't change).

### 6. Tear down infra-bootstrap-side resources

In infra-bootstrap, delete:

- `claude-container/` (entire directory).
- `mcp-servers/github/` (entire directory).
- `k8s/mcp-azure/`, `k8s/mcp-github/`.
- `k8s/apps/mcp-azure.yaml`, `k8s/apps/mcp-github.yaml` — **only after step 4**.
   (If you keep them as the source of truth for the Application registration but with flipped repoURL, skip this deletion. Either is fine.)
- `tofu/claude-container-ci.tf`, `tofu/mcp-github-ci.tf`.
- `tofu/mcp.tf`, `tofu/mcp-server/`.
- `.github/workflows/claude-container-build.yml`, `.github/workflows/mcp-github-build.yml`.
- The `mcp-servers/github/**` path filter in any other workflow that mentions it.

`tofu apply` in infra-bootstrap will then:

- Destroy the old `claude-container-ci` / `mcp-github-ci` SPs (and their federated creds + ACR role assignments).
- Destroy the old `mcp-azure-identity` UAMI (and its federated cred + Reader role + KV secret).
- Delete GH Actions vars `CLAUDE_CONTAINER_CI_CLIENT_ID` / `MCP_GITHUB_CI_CLIENT_ID` on `infra-bootstrap` (the new values now live on `tank-operator`).
- Delete the `mcp-tenant-id` KV secret. (tank-operator recreates it under its own state — the value is identical.)

### 7. Sanity check

- `kubectl get applications -n argocd mcp-azure mcp-github -o jsonpath='{.items[*].spec.source.repoURL}{"\n"}'` returns tank-operator URLs.
- `kubectl logs -n mcp-azure deploy/mcp-azure -c azure-mcp` shows successful authentication (no `AADSTS` errors).
- A fresh tank-operator session can call MCP tools (`/mcp` or invoking an azure tool) without 401.
