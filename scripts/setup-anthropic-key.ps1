# Stash an Anthropic API key in Azure Key Vault so session pods can launch
# claude CLI fully authenticated (TUI + all features).
#
# We use ANTHROPIC_API_KEY rather than the OAuth subscription path
# (CLAUDE_CODE_OAUTH_TOKEN) because the env-var token is "inference-only"
# and the full OAuth flow assumes a browser on the same machine — neither
# fits a headless container.
#
# Run on rotation. The script force-syncs the ExternalSecret so new session
# pods pick up the value immediately (no waiting on the 1h ESO poll).
#
# Usage:  .\scripts\setup-anthropic-key.ps1

$ErrorActionPreference = 'Stop'

$Vault         = if ($env:VAULT)          { $env:VAULT }          else { 'romaine-kv' }
$KvSecretName  = 'anthropic-api-key'
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
Storing your Anthropic API key in Azure Key Vault.

Get a key at https://console.anthropic.com/settings/keys (starts with `sk-ant-api...`).
Paste it below — input is hidden.
'@

$secure = Read-Host -Prompt "`nPaste API key" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $Token = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Error "empty value, aborting"
}

Write-Host ""
Write-Host "-> Writing Key Vault secret $Vault/$KvSecretName ..."
az keyvault secret set `
    --vault-name $Vault `
    --name $KvSecretName `
    --value $Token `
    --output none
if ($LASTEXITCODE -ne 0) { Write-Error "az keyvault secret set failed" }

Write-Host "-> Forcing ExternalSecret refresh on $EsoNamespace/$EsoName ..."
$ts = [int][double]::Parse((Get-Date -UFormat %s))
kubectl -n $EsoNamespace annotate externalsecret $EsoName "force-sync=$ts" --overwrite | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "kubectl annotate failed" }

Write-Host @'

Key stored. Newly created sessions will see ANTHROPIC_API_KEY in their env.

Note: pods that are already running will NOT pick up the new value (env vars
are captured at pod creation). Click the 'x' on the session tile to kill it,
then '+ new' for a fresh one.
'@
