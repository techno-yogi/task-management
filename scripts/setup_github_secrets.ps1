<#
.SYNOPSIS
  Bulk-imports a deploy secrets env-file into a GitHub Environment.

.DESCRIPTION
  Reads a KEY=VALUE file (supports multi-line values via VALUE_BEGIN/VALUE_END
  markers — see SSH_PRIVATE_KEY note below) and pushes each pair as an
  environment-scoped secret using `gh secret set --env <env> --repo <repo>`.

  Multi-line values (SSH private keys, ssh-keyscan output) are detected
  automatically: if the value on the same line as KEY= starts with "-----BEGIN"
  or you place "<<EOF" sentinel after =, the script keeps reading lines until
  the matching closing sentinel ("-----END ... -----" or "EOF" on its own line).

.PARAMETER EnvFile
  Path to the env file containing the secrets. See deploy/secrets.example.env.

.PARAMETER Environment
  GitHub Environment name to push to (e.g. staging, production).

.PARAMETER Repo
  owner/repo. Defaults to the repo of the current git remote `origin`.

.PARAMETER DryRun
  Print what would be done without calling `gh`.

.EXAMPLE
  pwsh ./scripts/setup_github_secrets.ps1 -EnvFile deploy/secrets.staging.env -Environment staging
#>
param(
  [Parameter(Mandatory=$true)][string]$EnvFile,
  [Parameter(Mandatory=$true)][string]$Environment,
  [string]$Repo,
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $EnvFile)) {
  throw "EnvFile not found: $EnvFile"
}

# Resolve repo from origin remote if not supplied.
if (-not $Repo) {
  $remote = git -C (Split-Path $PSScriptRoot -Parent) remote get-url origin 2>$null
  if (-not $remote) {
    throw "No -Repo supplied and no origin remote. Pass -Repo owner/name."
  }
  if ($remote -match 'github\.com[:/]([^/]+/[^/.]+)(\.git)?$') {
    $Repo = $Matches[1]
  } else {
    throw "Could not parse owner/repo from origin: $remote"
  }
}

Write-Host "Target: repo=$Repo environment=$Environment" -ForegroundColor Cyan
Write-Host "Source: $EnvFile" -ForegroundColor Cyan

# Verify gh auth.
$ghStatus = gh auth status 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
  Write-Error "gh CLI not authenticated. Run: gh auth login"
  exit 1
}

# Verify the environment exists (or create on first push — gh secret set
# auto-creates the environment).
$envCheck = gh api "repos/$Repo/environments/$Environment" 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Host "Environment '$Environment' does not exist yet on $Repo. Creating..." -ForegroundColor Yellow
  if (-not $DryRun) {
    gh api -X PUT "repos/$Repo/environments/$Environment" --silent
  }
}

# Parse the env file. Supports:
#   KEY=value                      (single-line)
#   KEY=-----BEGIN ...             (PEM block — read until -----END ... -----)
#   KEY=<<EOF                      (heredoc — read until line with just EOF)
$lines = Get-Content $EnvFile
$pairs = [ordered]@{}
$i = 0
while ($i -lt $lines.Count) {
  $line = $lines[$i]
  if ($line -match '^\s*$' -or $line -match '^\s*#') {
    $i++; continue
  }
  if ($line -notmatch '^([A-Z_][A-Z0-9_]*)=(.*)$') {
    Write-Warning "skipping malformed line $($i+1): $line"
    $i++; continue
  }
  $key = $Matches[1]
  $val = $Matches[2]

  if ($val -like '-----BEGIN*') {
    # PEM: collect until -----END line (inclusive).
    $buf = New-Object System.Text.StringBuilder
    [void]$buf.AppendLine($val)
    $i++
    while ($i -lt $lines.Count) {
      [void]$buf.AppendLine($lines[$i])
      if ($lines[$i] -like '-----END*') { $i++; break }
      $i++
    }
    $val = $buf.ToString().TrimEnd("`r","`n")
  }
  elseif ($val -eq '<<EOF') {
    $buf = New-Object System.Text.StringBuilder
    $i++
    while ($i -lt $lines.Count -and $lines[$i] -ne 'EOF') {
      [void]$buf.AppendLine($lines[$i])
      $i++
    }
    if ($i -ge $lines.Count) { throw "Unterminated heredoc for $key" }
    $i++  # consume EOF
    $val = $buf.ToString().TrimEnd("`r","`n")
  }
  else {
    $i++
  }

  $pairs[$key] = $val
}

Write-Host "Parsed $($pairs.Count) secret(s):" -ForegroundColor Cyan
foreach ($k in $pairs.Keys) {
  $v = $pairs[$k]
  if ($v -like '__*__' -or [string]::IsNullOrWhiteSpace($v)) {
    Write-Host ("  {0,-32} <PLACEHOLDER — refusing to push>" -f $k) -ForegroundColor Red
    $hasPlaceholders = $true
  } else {
    $preview = if ($v.Length -gt 24) { $v.Substring(0,12) + "..." + $v.Substring($v.Length-6) } else { ('*' * [Math]::Min($v.Length, 12)) }
    Write-Host ("  {0,-32} {1}" -f $k, $preview)
  }
}

if ($hasPlaceholders) {
  Write-Error "One or more secrets still contain placeholders (__...__). Fill them in first."
  exit 2
}

# Push each secret.
$pushed = 0
foreach ($k in $pairs.Keys) {
  $v = $pairs[$k]
  if ($DryRun) {
    Write-Host "[dry-run] gh secret set $k --env $Environment --repo $Repo (len=$($v.Length))" -ForegroundColor DarkGray
    continue
  }
  # Pipe via stdin so multi-line values survive intact and never appear in
  # process listings or shell history.
  $v | gh secret set $k --env $Environment --repo $Repo --body -
  if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to set $k (exit $LASTEXITCODE)"
    exit 3
  }
  $pushed++
}

if ($DryRun) {
  Write-Host "Dry-run complete. $($pairs.Count) secret(s) would be pushed." -ForegroundColor Green
} else {
  Write-Host "Pushed $pushed secret(s) to $Repo / environment $Environment." -ForegroundColor Green
  Write-Host "Verify with: gh api repos/$Repo/environments/$Environment/secrets" -ForegroundColor DarkGray
}
