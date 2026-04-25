# tank-operator

Web frontend over a thin K8s orchestrator that spawns ephemeral `claude-container` pods on
demand. "+ button → fresh agent shell, terminal opens in a browser tab, killed when the tab
closes." See [issue #1](https://github.com/nelsong6/tank-operator/issues/1) for the full
design and rationale.

## Repo layout

```
backend/                      FastAPI + kubernetes-asyncio orchestrator
frontend/                     Vite + React UI (xterm.js arrives in Phase 2)
Dockerfile                    multi-stage: vite build → python runtime
k8s/                          Helm chart: deployment, RBAC, HTTPRoute, ExternalSecret
.github/workflows/build.yml   OIDC az login → build → push to ACR
```

## Phases

1. **Skeleton** (this commit) — orchestrator Deployment up; `POST /api/sessions` creates a
   Job; `GET`/`DELETE` work; frontend `+` button hits the API and lists sessions. No exec.
2. **Exec** — WebSocket proxy + xterm.js. End-to-end terminal in browser.
3. **Polish** — tab UI, sidebar, idle reaper, optional per-session PVC.

## Local dev

```bash
# Backend (needs a kube context with access to the sessions namespace, or run --dry-run)
cd backend && pip install -e . && python -m tank_operator

# Frontend
cd frontend && npm install && npm run dev
# Vite dev server proxies /api → http://localhost:8000.
# Sign in via MSAL: the dev server uses the same Entra app registration as prod
# (redirect URI registered for https://tank.romaine.life/), so you'll need to
# either tunnel localhost behind that hostname or add a dev redirect URI.
```

## Deploy

ArgoCD auto-syncs `k8s/` when changes hit `main`. Image is built and pushed to
`romainecr.azurecr.io/tank-operator:<sha>` (and `:latest`) by `.github/workflows/build.yml`.

Auth: the SPA uses MSAL.js to obtain an Entra ID token, POSTs it to
`/api/auth/microsoft/login`, and the backend mints its own short-lived JWT
(see [auth.py](backend/src/tank_operator/auth.py)). Sessions are scoped by
SHA-256 of the signed-in user's email. Allowlist is the comma-separated
`ALLOWED_EMAILS` env var, sourced from KV via ExternalSecret.
