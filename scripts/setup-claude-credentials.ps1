# Stash a claude.ai subscription credentials blob in Azure Key Vault so session
# pods can launch the claude TUI fully authenticated against the user's
# subscription (no API-credit burn).
#
# We use the full ~/.claude/.credentials.json (with `user:sessions:claude_code`
# scope) rather than ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN: API keys
# bill API credits, and the env-var OAuth path is "inference-only" — only the
# credentials file satisfies the interactive TUI.
#
# To get the JSON: in WSL (or any Linux shell) where claude is installed, run
# `claude /login`, complete the browser flow, then `cat ~/.claude/.credentials.json`.
# Paste the entire blob (single line) into this script.
#
# Run on rotation. The script force-syncs the ExternalSecret so new session
# pods pick up the value immediately (no waiting on the 1h ESO poll).
#
# Usage:  .\scripts\setup-claude-credentials.ps1

$ErrorActionPreference = 'Stop'

$Vault         = if ($env:VAULT)          { $env:VAULT }          else { 'romaine-kv' }
$KvSecretName  = 'claude-credentials-json'
$EsoNamespace  = if ($env:ESO_NAMESPACE)  { $env:ESO_NAMESPACE }  else { 'tank-operator-sessions' }
$EsoName       = if ($env:ESO_NAME)       { $env:ESO_NAME }       else { 'github-app-creds' }

function Require-Cmd($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "'$name' is required but not on PATH"
    }
}
Require-Cmd az
Require-Cmd kubectl

Write-Host @'
Storing your claude.ai subscription credentials in Azure Key Vault.

In WSL (or any Linux shell) where claude is installed:
    claude /login          # complete the browser flow
    cat ~/.claude/.credentials.json

Paste the entire JSON blob below — input is hidden.
'@

$secure = Read-Host -Prompt "`nPaste credentials JSON" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $Blob = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($Blob)) {
    Write-Error "empty value, aborting"
}
try {
    $parsed = $Blob | ConvertFrom-Json
    if (-not $parsed.claudeAiOauth.refreshToken) { throw "missing .claudeAiOauth.refreshToken" }
} catch {
    Write-Error "input doesn't look like a credentials.json: $_"
}

Write-Host ""
Write-Host "-> Writing Key Vault secret $Vault/$KvSecretName ..."
az keyvault secret set `
    --vault-name $Vault `
    --name $KvSecretName `
    --value $Blob `
    --output none
if ($LASTEXITCODE -ne 0) { Write-Error "az keyvault secret set failed" }

Write-Host "-> Forcing ExternalSecret refresh on $EsoNamespace/$EsoName ..."
$ts = [int][double]::Parse((Get-Date -UFormat %s))
kubectl -n $EsoNamespace annotate externalsecret $EsoName "force-sync=$ts" --overwrite | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "kubectl annotate failed" }

Write-Host @'

Credentials stored. Newly created sessions will see CLAUDE_CREDENTIALS_JSON
in their env, and the bootstrap drops it at /root/.claude/.credentials.json.

Note: pods that are already running will NOT pick up the new value (env vars
are captured at pod creation). Click the 'x' on the session tile to kill it,
then '+ new' for a fresh one.
'@
