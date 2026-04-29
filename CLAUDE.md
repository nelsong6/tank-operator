# tank-operator

Web frontend over a thin K8s orchestrator that spawns ephemeral session pods on demand. "+ button → fresh container, terminal opens in a browser tab, killed when the tab closes." See [README.md](README.md) for the layout.

## Product position

**The pod is the product**, not any specific agent UI. Pods ship pre-configured with private networking + an MCP gateway + (eventually) docs RAG; the user runs whatever agent they prefer inside it (Claude Code today because it's common, but Codex/Gemini/aider/`vim` should all work). North Star is "don't make users learn our system" — they bring their existing tooling into a pre-baked environment. Closest analog is Coder, with MCP-aware envs as the wedge no one else ships.

**Strategic purpose — LLMs-as-a-service.** Beyond personal use, the platform is designed to hand a hosted dev session to other people. Near-term: collaborate with friends without their own Claude Max subscription (gated by `ALLOWED_EMAIL`). Long-term: enterprise multi-tenant LLM delivery — same shape, with billing/quotas/tenancy. Design preserves this: per-session pod isolation, SA-token-scoped MCP access, no shared filesystem state, email-allowlist auth that can grow into roles/orgs.

**Why not Anthropic Managed Agents (shipped 2026-04-08)?** Covers most of the env-spec primitives but is (a) Claude-only — breaks the agent-agnostic position, (b) API-billed per-token — breaks the "share my Max subscription with friends" economics, (c) explicitly forbids shipping a Claude-Code-styled UI. Tank-operator's defensible edges are the subscription-token sharing model and the agent-agnostic shape; the runtime layer itself is now commoditized.

## Stack

FastAPI + kubernetes-asyncio backend, Vite + React + xterm.js frontend, multi-stage Dockerfile, Helm chart in `k8s/` synced by ArgoCD. Two namespaces: `tank-operator` (long-lived orchestrator Deployment) and `tank-operator-sessions` (ephemeral session Deployments).

## Terminal

In-house xterm.js + K8s pods/exec bridge (`backend/src/tank_operator/exec_proxy.py`) with `CLAUDE_CODE_NO_FLICKER=1` set in the session pod env to enable Claude Code's alt-screen-buffer renderer (works around the Ink redraw leak in `anthropics/claude-code#49086` and `#29937` — source of the ghost-text and post-resize collision symptoms). The bridge does meaningful work beyond byte-shuffling: bootstrap shell that seeds claude state to skip onboarding prompts (lives in the image at `/opt/tank/bootstrap.sh` per `claude-container/tank-bootstrap.sh` — keeps the apiserver exec URL small), tmux session for reconnect survival, MCP bearer token export from the projected SA token, mode-aware credential setup. ttyd-in-pod is a viable alternative — defer until/unless the in-house bridge proves to need protocol features ttyd has and we don't.

**Session pods are multi-container** (`claude` + `mcp-auth-proxy` sidecar). Any `pods/exec` call against them MUST pass `container="claude"` — the apiserver returns 400 "a container name must be specified" otherwise, which surfaces to the browser as a 1006 reconnect loop. Same gotcha for ad-hoc `kubectl exec` debugging: use `-c claude`.

The browser xterm.js terminal is *the* route, not a demo surface. SSH / VS Code Remote attach are not on the roadmap as alternatives. Rendering glitches matter because users can't route around them.

## Auth flow

- Browser SPA uses MSAL.js (auth-code+PKCE) to obtain an Entra ID token from a public app reg (`tank-operator-oauth`, distinct from the CI app). Bootstrap config (`entra_client_id`, authority) comes from the public `/api/config` endpoint.
- SPA POSTs the token to `/api/auth/microsoft/login`. Backend validates via JWKS at `login.microsoftonline.com/common/...` (regex issuer match — permissive; `ALLOWED_EMAIL` env var is the gate), then mints its own HS256 JWT signed with `JWT_SECRET` (7-day TTL).
- Session JWT comes back as response body (frontend → localStorage) and as an httpOnly cookie (`auth_token`). REST uses Bearer; WebSocket uses the cookie since browsers can't set Authorization on WS upgrades.
- `current_user` re-checks the email against `ALLOWED_EMAIL` on every protected endpoint, so revoking access only needs a tofu apply, not a token rotation.
- No oauth2-proxy. Session pods authenticate to in-cluster MCP servers via the projected SA token (read fresh per request by the `mcp-auth-proxy` sidecar in each session pod); MCP servers do Azure work via their dedicated UAMIs.

## In-cluster MCP servers

`mcp-servers/<name>/` (Python source) + `k8s-mcp-<name>/` (Helm chart with `kube-rbac-proxy` sidecar). Inbound auth: claude-session SA token validated via TokenReview + SubjectAccessReview against the synthetic `mcp.tank-operator.io/servers/<name>` resource. Currently:

- `mcp-azure` — wraps Microsoft's `azure-mcp` image
- `mcp-github` — custom GitHub-App-backed
- `mcp-k8s` — read-only kubectl/helm
- `mcp-argocd` — read-only ArgoCD via Dex SA-token exchange (no static API tokens)

### azure-mcp config keys

The `mcr.microsoft.com/azure-sdk/azure-mcp` image binds inbound JWT validation and the OAuth metadata document from **ASP.NET Core hierarchical config keys**, not the `AZURE_AD_*` names some Microsoft Bicep templates use. Source: `microsoft/mcp` repo → `Microsoft.Mcp.Core/src/Areas/Server/Commands/ServiceStartCommand.cs`.

- **Inbound auth + metadata:** `AzureAd__Instance`, `AzureAd__TenantId`, `AzureAd__ClientId`
- **Outgoing OBO calls (DefaultAzureCredential / WorkloadIdentityCredential):** `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` (the resource Entra app's clientID — federation is on the resource app, not a separate MI), `AZURE_FEDERATED_TOKEN_FILE`, `AZURE_AUTHORITY_HOST`
- `AZURE_MCP_DANGEROUSLY_ENABLE_FORWARDED_HEADERS=true` — required behind any TLS-terminating proxy so the OAuth metadata advertises `https://` resource URLs.
- The image entrypoint is already `./server-binary server start`, so pod `args:` should only contain flags. Default ASP.NET Core bind is `localhost:5000` — set `ASPNETCORE_URLS=http://+:8080` (or your port) so kubelet probes and the Service can reach it.
