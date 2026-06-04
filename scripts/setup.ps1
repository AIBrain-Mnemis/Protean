# All-in-one setup for Protean (Windows / PowerShell).
#
# Idempotent: every step checks before it acts, so re-running is safe.
# Each step prints PASS / SKIP / FAIL; exit code is 1 if any required step
# fails. Interactive only — no flags.
#
# Env:
#   PROTEAN_REPO_URL   git URL used when the script is run outside a checkout

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'  # collect a final status, don't bail
$script:Failed = $false

# ---------- pretty output ------------------------------------------------
function Write-Step  { param($Msg) Write-Host "`n> $Msg" -ForegroundColor Blue }
function Write-Pass  { param($Msg) Write-Host "  PASS $Msg" -ForegroundColor Green }
function Write-Skip  { param($Msg) Write-Host "  SKIP $Msg" -ForegroundColor Yellow }
function Write-Warn  { param($Msg) Write-Host "  WARN $Msg" -ForegroundColor Yellow }
function Write-Fail  { param($Msg) Write-Host "  FAIL $Msg" -ForegroundColor Red; $script:Failed = $true }
function Write-Info  { param($Msg) Write-Host "       $Msg" }

function Ask-YesNo {
    param([string]$Prompt, [string]$Default = 'y')
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
        return ($Default -eq 'y')
    }
    $hint = if ($Default -eq 'y') { '[Y/n]' } else { '[y/N]' }
    $reply = Read-Host "       $Prompt $hint"
    if ([string]::IsNullOrWhiteSpace($reply)) { $reply = $Default }
    return $reply -match '^(y|yes)$'
}

function Ask-Secret {
    param([string]$Prompt)
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
        return ''
    }
    $secure = Read-Host "       $Prompt (input hidden)" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Ask-Input {
    param([string]$Prompt, [string]$Default = '')
    if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
        return $Default
    }
    $hint = if ($Default) { " [$Default]" } else { ' (blank to skip)' }
    $reply = Read-Host "       $Prompt$hint"
    if ([string]::IsNullOrWhiteSpace($reply)) { return $Default }
    return $reply
}

function Env-Get {
    param([string]$Key)
    if (-not (Test-Path .env)) { return '' }
    foreach ($line in Get-Content .env) {
        if ($line -match "^\s*$([regex]::Escape($Key))=(.*)$") {
            return $matches[1].Trim().Trim("'").Trim('"')
        }
    }
    return ''
}

function Env-Set {
    param([string]$Key, [string]$Value)
    $lines = if (Test-Path .env) { Get-Content .env } else { @() }
    $found = $false
    $out = foreach ($line in $lines) {
        if ($line -match "^\s*$([regex]::Escape($Key))=") {
            $found = $true
            "$Key=$Value"
        } else {
            $line
        }
    }
    if (-not $found) { $out += "$Key=$Value" }
    Set-Content -Path .env -Value $out -Encoding UTF8
}

function Test-Cmd { param($Name) [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Test-ProteanRepo {
    param([string]$Dir)
    if (-not $Dir -or -not (Test-Path (Join-Path $Dir 'pyproject.toml'))) { return $false }
    return (Select-String -Path (Join-Path $Dir 'pyproject.toml') `
        -Pattern '^\s*name\s*=\s*"protean"' -Quiet -ErrorAction SilentlyContinue)
}

# ---------- locate or clone repo ----------------------------------------
# If this script lives inside a protean repo, use it. Otherwise (downloaded
# standalone), offer to git-clone into ~/.protean.
$ScriptDir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$CandidateRoot  = if ($ScriptDir) { Resolve-Path (Join-Path $ScriptDir '..') -ErrorAction SilentlyContinue } else { $null }

if ($CandidateRoot -and (Test-ProteanRepo $CandidateRoot.Path)) {
    $RepoRoot = $CandidateRoot.Path
    Write-Host "Protean setup (root: $RepoRoot)" -ForegroundColor White
} else {
    Write-Host "Protean setup (no local repo detected)" -ForegroundColor White
    Write-Step "Clone Protean repository"
    if (-not (Test-Cmd 'git')) {
        Write-Fail "git not found — install git and re-run"
        exit 1
    }
    $defaultDir = Join-Path $HOME '.protean'
    $defaultUrl = if ($env:PROTEAN_REPO_URL) { $env:PROTEAN_REPO_URL } else { 'https://github.com/AIBrain-Mnemis/Protean.git' }
    $url = Ask-Input "Git URL" $defaultUrl
    if (-not $url) {
        Write-Fail "no git URL provided"
        exit 1
    }
    $target = Ask-Input "Clone destination" $defaultDir
    if (Test-Path (Join-Path $target '.git')) {
        Write-Pass "$target already cloned, reusing"
    } elseif ((Test-Path $target) -and (Get-ChildItem -Force -Path $target -ErrorAction SilentlyContinue)) {
        Write-Fail "$target exists and is not empty — pick another path"
        exit 1
    } else {
        git clone $url $target
        if ($LASTEXITCODE -ne 0) { Write-Fail "git clone failed"; exit 1 }
        Write-Pass "cloned into $target"
    }
    $RepoRoot = $target
}

Set-Location $RepoRoot

# ---------- uv -----------------------------------------------------------
Write-Step "Check uv (Python package manager)"
if (Test-Cmd 'uv') {
    $uvVer = (uv --version) -split ' ' | Select-Object -Index 1
    Write-Pass "uv $uvVer"
} else {
    Write-Warn "uv not found"
    if (Ask-YesNo "Install uv now via the official installer?" 'y') {
        # Official PowerShell installer from astral.sh
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        # The installer writes to %USERPROFILE%\.local\bin; refresh PATH for this session.
        $uvBin = Join-Path $env:USERPROFILE '.local\bin'
        if (Test-Path (Join-Path $uvBin 'uv.exe')) {
            $env:PATH = "$uvBin;$env:PATH"
        }
        if (Test-Cmd 'uv') {
            $uvVer = (uv --version) -split ' ' | Select-Object -Index 1
            Write-Pass "uv $uvVer (installed)"
            Write-Warn "Restart your shell so future sessions pick up uv on PATH"
        } else {
            Write-Fail "uv installed but not on PATH — restart your shell and re-run setup"
        }
    } else {
        Write-Fail "uv required; install manually: irm https://astral.sh/uv/install.ps1 | iex"
    }
}

# ---------- python deps --------------------------------------------------
Write-Step "Sync Python dependencies (uv sync)"
if (Test-Cmd 'uv') {
    & uv sync
    if ($LASTEXITCODE -eq 0) { Write-Pass "uv sync" } else { Write-Fail "uv sync failed" }
} else {
    Write-Skip "uv missing (see previous step)"
}

# ---------- .env ---------------------------------------------------------
Write-Step "Bootstrap .env"
if (Test-Path .env) {
    Write-Pass ".env already present"
} elseif (Test-Path .env.example) {
    Copy-Item .env.example .env
    Write-Pass "created .env from .env.example"
} else {
    Write-Fail ".env.example missing — cannot create .env"
}

# ---------- ffmpeg -------------------------------------------------------
Write-Step "Check ffmpeg (provides ffmpeg + ffprobe)"
if ((Test-Cmd 'ffmpeg') -and (Test-Cmd 'ffprobe')) {
    Write-Pass "ffmpeg + ffprobe present"
} else {
    Write-Fail "ffmpeg / ffprobe missing"
    Write-Info "Install: winget install Gyan.FFmpeg  (or: choco install ffmpeg)"
}

# ---------- node ---------------------------------------------------------
Write-Step "Check Node.js (for electron-bridge / realtime path)"
$NodeOK = $false
if (Test-Cmd 'node') {
    $nodeVer = (node -v).TrimStart('v')
    $nodeMajor = [int]($nodeVer -split '\.')[0]
    if ($nodeMajor -ge 22) {
        Write-Pass "node v$nodeVer (>= 22, realtime path supported)"
        $NodeOK = $true
    } elseif ($nodeMajor -ge 18) {
        Write-Warn "node v$nodeVer (>= 18, CUA terminal tool only — realtime needs >= 22)"
    } else {
        Write-Fail "node v$nodeVer too old (need >= 22 for realtime, >= 18 for CUA terminal)"
    }
} else {
    Write-Warn "node not found — needed only for realtime voice path"
    Write-Info "Install Node 22+ via 'winget install OpenJS.NodeJS.LTS' or https://nodejs.org"
    Write-Info "Or set PROTEAN_CUA_TERMINAL=false to skip Node entirely"
}

# ---------- electron-bridge build ----------------------------------------
Write-Step "Build electron-bridge (realtime voice / screen-share)"
if (-not (Test-Path electron-bridge)) {
    Write-Skip "electron-bridge/ not present"
} elseif (-not $NodeOK) {
    Write-Skip "Node >= 22 required (see previous step)"
} else {
    Write-Info "Required only for the realtime voice / screen-share daemon."
    if (Ask-YesNo "Build electron-bridge now?" 'y') {
        Push-Location electron-bridge
        try {
            if (-not (Test-Path node_modules)) {
                Write-Info "npm install ..."
                & npm install --silent
                if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
            } else {
                Write-Info "node_modules present, skipping npm install"
            }
            Write-Info "npm run build ..."
            & npm run build --silent
            if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
            Write-Pass "electron-bridge built"
        } catch {
            Write-Fail "electron-bridge build failed: $_"
        } finally {
            Pop-Location
        }
    } else {
        Write-Skip "user declined"
    }
}

# ---------- agents wiring ------------------------------------------------
Write-Step "Wire external agent runtimes (Codex / Claude Code)"
function Wire-Agent {
    param([string]$Agent)
    Write-Info "agents setup $Agent ..."
    & uv run protean agents setup $Agent
    if ($LASTEXITCODE -eq 0) { Write-Pass "wired $Agent" } else { Write-Fail "agents setup $Agent failed" }
}

if (-not (Test-Cmd 'uv')) {
    Write-Skip "uv missing — cannot run 'protean agents setup'"
} else {
    Write-Info "Skips an agent automatically when its home directory is absent."
    $userProfile = $env:USERPROFILE
    $candidates = @(
        @{ Kind = 'codex';       Label = 'Codex';       Home = if ($env:CODEX_HOME)  { $env:CODEX_HOME }  else { Join-Path $userProfile '.codex' } },
        @{ Kind = 'claude_code'; Label = 'Claude Code'; Home = if ($env:CLAUDE_HOME) { $env:CLAUDE_HOME } else { Join-Path $userProfile '.claude' } }
    )
    foreach ($c in $candidates) {
        if (-not (Test-Path $c.Home)) {
            Write-Skip "$($c.Label) not installed ($($c.Home) missing)"
            continue
        }
        if (Ask-YesNo "Wire $($c.Label) ($($c.Home))?" 'y') {
            Wire-Agent $c.Kind
        } else {
            Write-Skip "$($c.Label) declined"
        }
    }
}

# ---------- .env: default LLM provider (interactive) --------------------
$providerMeta = @{
    openai    = @{ Key = 'OPENAI_API_KEY';    Model = 'OPENAI_MODEL';    Default = 'gpt-5' }
    anthropic = @{ Key = 'ANTHROPIC_API_KEY'; Model = 'ANTHROPIC_MODEL'; Default = 'claude-sonnet-4-5' }
    gemini    = @{ Key = 'GEMINI_API_KEY';    Model = 'GEMINI_MODEL';    Default = 'gemini-3.5-flash' }
    doubao    = @{ Key = 'ARK_API_KEY';       Model = 'ARK_MODEL';       Default = 'doubao-pro-256k' }
}

Write-Step "Configure default LLM provider in .env"
$currentProvider = Env-Get 'PROTEAN_DEFAULT_PROVIDER'
if ($currentProvider) {
    Write-Info "Current: PROTEAN_DEFAULT_PROVIDER=$currentProvider"
    $doConfigure = Ask-YesNo "Change the default provider?" 'n'
    if (-not $doConfigure) { Write-Skip "kept existing" }
} else {
    $doConfigure = Ask-YesNo "Configure the default provider now?" 'y'
    if (-not $doConfigure) { Write-Skip "default provider (declined)" }
}
if ($doConfigure) {
    $curIdx = switch ($currentProvider) {
        'openai'    { '1' }
        'anthropic' { '2' }
        'gemini'    { '3' }
        'doubao'    { '4' }
        default     { '1' }
    }
    Write-Info "Options: 1) openai   2) anthropic   3) gemini   4) doubao"
    $choice = Ask-Input "Pick the default provider [1-4]" $curIdx
    $provider = switch ($choice) {
        { $_ -in '1', 'openai' }    { 'openai' }
        { $_ -in '2', 'anthropic' } { 'anthropic' }
        { $_ -in '3', 'gemini' }    { 'gemini' }
        { $_ -in '4', 'doubao' }    { 'doubao' }
        default { Write-Fail "unknown choice: $choice"; $null }
    }
    if ($provider) {
        $meta = $providerMeta[$provider]
        $currentKey   = Env-Get $meta.Key
        $currentModel = Env-Get $meta.Model
        if ($currentKey) {
            $secret = Ask-Secret "$provider API key (blank to keep existing)"
        } else {
            $secret = Ask-Secret "$provider API key"
        }
        if ([string]::IsNullOrEmpty($secret) -and -not $currentKey) {
            Write-Fail "$($meta.Key) (empty input) — default provider not written"
        } else {
            Env-Set 'PROTEAN_DEFAULT_PROVIDER' $provider
            if ($secret) {
                Env-Set $meta.Key $secret
                Write-Pass "PROTEAN_DEFAULT_PROVIDER=$provider, $($meta.Key) updated"
            } else {
                Write-Pass "PROTEAN_DEFAULT_PROVIDER=$provider, $($meta.Key) kept"
            }
            $modelDefault = if ($currentModel) { $currentModel } else { $meta.Default }
            $model = Ask-Input "$provider model" $modelDefault
            Env-Set $meta.Model $model
            Write-Pass "$($meta.Model)=$model"
        }
    }
}

# ---------- .env: realtime voice daemon (optional) ----------------------
Write-Step "Configure realtime voice daemon in .env (optional)"
$currentRealtime = Env-Get 'PROTEAN_REALTIME_MODEL'
if ($currentRealtime) {
    Write-Info "Current: PROTEAN_REALTIME_MODEL=$currentRealtime"
    $doConfigure = Ask-YesNo "Change realtime configuration?" 'n'
    if (-not $doConfigure) { Write-Skip "kept existing" }
} else {
    Write-Info "Realtime voice / screen-share runs through Gemini Live."
    $doConfigure = Ask-YesNo "Enable the realtime daemon now?" 'n'
    if (-not $doConfigure) { Write-Skip "realtime (declined)" }
}
if ($doConfigure) {
    $currentGemini = Env-Get 'GEMINI_API_KEY'
    if ($currentGemini) {
        $geminiKey = Ask-Secret "Gemini API key (blank to keep existing)"
        if ($geminiKey) {
            Env-Set 'GEMINI_API_KEY' $geminiKey
            Write-Pass "GEMINI_API_KEY updated"
        } else {
            Write-Pass "GEMINI_API_KEY kept"
        }
    } else {
        $geminiKey = Ask-Secret "Gemini API key"
        if ([string]::IsNullOrEmpty($geminiKey)) {
            Write-Fail "GEMINI_API_KEY (empty input) — realtime not enabled"
        } else {
            Env-Set 'GEMINI_API_KEY' $geminiKey
            Write-Pass "GEMINI_API_KEY set"
        }
    }
    if (Env-Get 'GEMINI_API_KEY') {
        $defaultRealtime = 'gemini-3.1-flash-live-preview'
        $modelDefault = if ($currentRealtime) { $currentRealtime } else { $defaultRealtime }
        $model = Ask-Input "Realtime model" $modelDefault
        Env-Set 'PROTEAN_REALTIME_MODEL' $model
        Write-Pass "PROTEAN_REALTIME_MODEL=$model"
    }
}

# ---------- .env: speech transcription (optional) -----------------------
Write-Step "Configure speech transcription in .env (optional)"
$currentAsrUrl = Env-Get 'PROTEAN_ASR_URL'
if ($currentAsrUrl) {
    Write-Info "Current: PROTEAN_ASR_URL=$currentAsrUrl"
    Write-Info "         PROTEAN_ASR_MODEL=$(Env-Get 'PROTEAN_ASR_MODEL')"
    $doConfigure = Ask-YesNo "Change transcription configuration?" 'n'
    if (-not $doConfigure) { Write-Skip "kept existing" }
} else {
    Write-Info "Transcribes mic audio captured during recording."
    Write-Info "Needs an OpenAI-compatible /v1/audio/transcriptions endpoint."
    $doConfigure = Ask-YesNo "Enable speech transcription now?" 'n'
    if (-not $doConfigure) { Write-Skip "speech transcription (declined)" }
}
if ($doConfigure) {
    $asrUrl = Ask-Input "ASR endpoint URL" $currentAsrUrl
    if ([string]::IsNullOrEmpty($asrUrl)) {
        Write-Fail "PROTEAN_ASR_URL (empty input) — transcription not enabled"
    } else {
        Env-Set 'PROTEAN_ASR_URL' $asrUrl
        Write-Pass "PROTEAN_ASR_URL=$asrUrl"
        $currentAsrModel = Env-Get 'PROTEAN_ASR_MODEL'
        $asrModelDefault = if ($currentAsrModel) { $currentAsrModel } else { 'whisper-1' }
        $asrModel = Ask-Input "ASR model" $asrModelDefault
        Env-Set 'PROTEAN_ASR_MODEL' $asrModel
        Write-Pass "PROTEAN_ASR_MODEL=$asrModel"
        $currentAsrKey = Env-Get 'PROTEAN_ASR_API_KEY'
        if ($currentAsrKey) {
            $asrKey = Ask-Secret "ASR API key (blank to keep existing)"
            if ($asrKey) {
                Env-Set 'PROTEAN_ASR_API_KEY' $asrKey
                Write-Pass "PROTEAN_ASR_API_KEY updated"
            } else {
                Write-Pass "PROTEAN_ASR_API_KEY kept"
            }
        } else {
            $asrKey = Ask-Secret "ASR API key (blank for self-hosted)"
            if ($asrKey) {
                Env-Set 'PROTEAN_ASR_API_KEY' $asrKey
                Write-Pass "PROTEAN_ASR_API_KEY set"
            } else {
                Write-Skip "PROTEAN_ASR_API_KEY (blank — fine for self-hosted)"
            }
        }
    }
}

# ---------- Windows-specific reminder -----------------------------------
Write-Step "Windows notes"
Write-Info "Run Protean from an interactive desktop session (UIA needs a real GUI session)."
Write-Info "Defender / SmartScreen may prompt the first time the electron-bridge .exe runs."

# ---------- summary ------------------------------------------------------
Write-Host ""
if (-not $script:Failed) {
    Write-Host "Setup complete." -ForegroundColor Green
    Write-Host "Next: uv run protean --help" -ForegroundColor White
    exit 0
} else {
    Write-Host "Setup finished with failures. Address the FAIL items above and re-run." -ForegroundColor Red
    exit 1
}
