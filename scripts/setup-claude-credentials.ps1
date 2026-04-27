# Seed the Claude Code subscription credentials in Azure Key Vault. From there
# ExternalSecret pulls them once into a K8s Secret in the orchestrator
# namespace; the in-cluster OAuth gateway (POST /v1/oauth/token in
# tank-operator's FastAPI app) is the only thing that reads or writes that
# Secret going forward. Session pods never see the refresh token.
#
# How to produce the JSON:
#   1. In WSL (or any Linux env), `npm i -g @anthropic-ai/claude-code`.
#   2. Run `claude` and complete `/login` in a browser.
#   3. `cat ~/.claude/.credentials.json` — that's the blob this script wants.
#
# When to re-run: only when the refresh chain dies entirely (e.g. you revoked
# access manually, or the gateway has been off long enough that the refresh
# token expired). In normal operation the gateway rotates the refresh token
# in the K8s Secret on every refresh, so KV drifts intentionally — KV is the
# disaster-recovery seed, not the live source of truth.
#
# Usage:  Get-Content path\to\credentials.json | .\scripts\setup-claude-credentials.ps1
#    or:  .\scripts\setup-claude-credentials.ps1   (then paste + Ctrl-Z + Enter)

$ErrorActionPreference = 'Stop'

$Vault         = if ($env:VAULT)         { $env:VAULT }         else { 'romaine-kv' }
$KvSecretName  = 'claude-code-credentials'
# After re-seeding KV we force-sync the ExternalSecret that lives in the
# orchestrator namespace (refreshInterval: 0s, so without an explicit poke
# ESO never re-reads from KV).
$EsoNamespace  = if ($env:ESO_NAMESPACE) { $env:ESO_NAMESPACE } else { 'tank-operator' }
$EsoName       = if ($env:ESO_NAME)      { $env:ESO_NAME }      else { 'claude-code-credentials' }

function Require-Cmd($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "'$name' is required but not on PATH"
    }
}
Require-Cmd az
Require-Cmd kubectl

if ([Console]::IsInputRedirected) {
    $Json = [Console]::In.ReadToEnd()
} else {
    Write-Host @'
Storing your Claude Code subscription credentials in Azure Key Vault.

Paste the contents of ~/.claude/.credentials.json below, then press Ctrl-Z and Enter.
(Generate by running `claude` in WSL/Linux and completing /login.)
'@
    $lines = @()
    while ($null -ne ($line = [Console]::In.ReadLine())) { $lines += $line }
    $Json = ($lines -join "`n")
}

if ([string]::IsNullOrWhiteSpace($Json)) {
    Write-Error "empty input, aborting"
}

try {
    [void]($Json | ConvertFrom-Json)
} catch {
    Write-Error "input is not valid JSON, aborting"
}

# Write JSON to a temp file because passing a multi-line value through `az`
# arguments is fraught (newlines, quoting, length limits).
$tmp = New-TemporaryFile
try {
    [IO.File]::WriteAllText($tmp.FullName, $Json)

    Write-Host ""
    Write-Host "-> Writing Key Vault secret $Vault/$KvSecretName ..."
    az keyvault secret set `
        --vault-name $Vault `
        --name $KvSecretName `
        --file $tmp.FullName `
        --output none
    if ($LASTEXITCODE -ne 0) { Write-Error "az keyvault secret set failed" }
} finally {
    Remove-Item $tmp.FullName -Force -ErrorAction SilentlyContinue
}

Write-Host "-> Forcing ExternalSecret refresh on $EsoNamespace/$EsoName ..."
$ts = [int][double]::Parse((Get-Date -UFormat %s))
kubectl -n $EsoNamespace annotate externalsecret $EsoName "force-sync=$ts" --overwrite | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "kubectl annotate failed" }

Write-Host @'

Credentials seeded. The OAuth gateway will pick up the new refresh token
on its next call to platform.claude.com.

Note: the orchestrator pod caches the refresh token in memory after first
read; restart it (kubectl -n tank-operator rollout restart deploy/tank-operator)
to force an immediate re-read. Existing session pods are unaffected — they
talk to the gateway, not to KV.
'@
