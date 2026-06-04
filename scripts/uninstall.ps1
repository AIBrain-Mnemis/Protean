# All-in-one teardown for Protean (Windows / PowerShell). Inverse of setup.ps1.
#
# Non-interactive: removes everything setup.ps1 creates, in order.
# Shared tools (uv, ffmpeg, node) are never touched.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$script:Failed = $false

# ---------- pretty output ------------------------------------------------
function Write-Step { param($Msg) Write-Host "`n> $Msg" -ForegroundColor Blue }
function Write-Pass { param($Msg) Write-Host "  PASS $Msg" -ForegroundColor Green }
function Write-Skip { param($Msg) Write-Host "  SKIP $Msg" -ForegroundColor Yellow }
function Write-Fail { param($Msg) Write-Host "  FAIL $Msg" -ForegroundColor Red; $script:Failed = $true }

function Test-Cmd { param($Name) [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

# ---------- locate repo --------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir '..')).Path
Set-Location $RepoRoot
Write-Host "Protean uninstall (root: $RepoRoot)" -ForegroundColor White

# ---------- 1. unwire external agents -----------------------------------
Write-Step "Unwire external agent runtimes (Codex / Claude Code)"
if (-not (Test-Cmd 'uv')) {
    Write-Skip "uv missing — cannot run 'protean agents uninstall'"
} else {
    & uv run protean agents uninstall all
    if ($LASTEXITCODE -eq 0) { Write-Pass "agents uninstall all" } else { Write-Fail "agents uninstall all failed" }
}

# ---------- 2. electron-bridge build artifacts --------------------------
Write-Step "Remove electron-bridge build artifacts"
if (-not (Test-Path electron-bridge)) {
    Write-Skip "electron-bridge/ not present"
} else {
    foreach ($d in @('electron-bridge\node_modules', 'electron-bridge\dist')) {
        if (Test-Path $d) {
            Remove-Item -Recurse -Force $d
            Write-Pass "removed $d"
        }
    }
}

# ---------- 3. .venv ----------------------------------------------------
Write-Step "Remove Python virtual environment (.venv)"
if (Test-Path .venv) {
    Remove-Item -Recurse -Force .venv
    Write-Pass "removed .venv"
} else {
    Write-Skip ".venv not present"
}

# ---------- 4. .env -----------------------------------------------------
Write-Step "Remove .env (provider keys + model overrides)"
if (Test-Path .env) {
    Remove-Item -Force .env
    Write-Pass "removed .env"
} else {
    Write-Skip ".env not present"
}

# ---------- 5. ~/.protean clone (only if we live there) -----------------
$defaultClone = Join-Path $HOME '.protean'
if ($RepoRoot -eq $defaultClone) {
    Write-Step "Remove cloned repository ($RepoRoot)"
    # Schedule self-deletion via a detached PowerShell so we can rm our own cwd.
    Start-Process powershell -ArgumentList @(
        '-NoProfile', '-WindowStyle', 'Hidden', '-Command',
        "Start-Sleep -Seconds 1; Remove-Item -Recurse -Force '$RepoRoot'"
    ) | Out-Null
    Write-Pass "scheduled removal of $RepoRoot"
}

# ---------- summary -----------------------------------------------------
Write-Host ""
if (-not $script:Failed) {
    Write-Host "Uninstall complete." -ForegroundColor Green
    Write-Host "Shared tools (uv, ffmpeg, node) were left alone." -ForegroundColor White
    exit 0
} else {
    Write-Host "Uninstall finished with failures. See FAIL items above." -ForegroundColor Red
    exit 1
}
