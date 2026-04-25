# Stash a Claude Code subscription OAuth token in Azure Key Vault so session
# pods can use the user's claude.ai subscription instead of an API key.
#
# Run this once per token rotation (the token is good for ~1 year). It
# refreshes the ExternalSecret immediately so newly-created session pods
# pick up the new value without waiting for the 1h ESO poll.
#
# Usage:  .\scripts\setup-claude-token.ps1

$ErrorActionPreference = 'Stop'

$Vault         = if ($env:VAULT)          { $env:VAULT }          else { 'romaine-kv' }
$KvSecretName  = 'claude-code-oauth-token'
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
Generating a long-lived OAuth token tied to your claude.ai subscription.

In another terminal (or below), run:
    claude setup-token

It will open a browser, you'll authenticate against claude.ai, and the CLI
will print a token (starts with `sk-ant-oat-...`). Copy it and paste it here.
'@

$secure = Read-Host -Prompt "`nPaste token" -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $Token = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Error "empty token, aborting"
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

Token stored. Newly created sessions will see CLAUDE_CODE_OAUTH_TOKEN.

Note: pods that are already running will NOT pick up the new value (the env
var is captured at pod creation). Click the 'x' on the session tile to kill
it, then '+ new' for a fresh one.
'@
