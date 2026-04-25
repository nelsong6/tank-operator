# Stash a Claude Code subscription credentials JSON in Azure Key Vault so
# session pods can launch claude as a logged-in subscriber instead of paying
# per-token via an API key.
#
# How to produce the JSON:
#   1. In WSL (or any Linux env), `npm i -g @anthropic-ai/claude-code`.
#   2. Run `claude` and complete `/login` in a browser.
#   3. `cat ~/.claude/.credentials.json` — that's the blob this script wants.
#
# The pod's bootstrap writes the same JSON to /root/.claude/.credentials.json
# before launching claude, so the TUI treats the container as a logged-in
# subscriber. The CLI auto-refreshes inside the pod, but those refreshes
# don't get back to KV — every fresh pod starts from the snapshot here.
# Re-run when sessions stop authenticating.
#
# Usage:  Get-Content path\to\credentials.json | .\scripts\setup-claude-credentials.ps1
#    or:  .\scripts\setup-claude-credentials.ps1   (then paste + Ctrl-Z + Enter)

$ErrorActionPreference = 'Stop'

$Vault         = if ($env:VAULT)         { $env:VAULT }         else { 'romaine-kv' }
$KvSecretName  = 'claude-code-credentials'
$EsoNamespace  = if ($env:ESO_NAMESPACE) { $env:ESO_NAMESPACE } else { 'tank-operator-sessions' }
$EsoName       = if ($env:ESO_NAME)      { $env:ESO_NAME }      else { 'github-app-creds' }

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

Credentials stored. New "subscription" sessions will boot logged-in.

Note: pods already running will NOT see the new value. Kill + recreate.
'@
