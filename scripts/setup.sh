#!/usr/bin/env bash
# All-in-one setup for Protean.
#
# Idempotent: every step checks before it acts, so re-running this script
# is safe. Each step prints PASS / SKIP / FAIL; the script exits 1 if any
# required step fails.
#
# Interactive only — prompts for every optional step (electron-bridge,
# external agent runtimes, provider keys). No flags.
#
# Env:
#   PROTEAN_REPO_URL   git URL used when the script is run outside a checkout

set -u

# ---------- pretty output ------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
  C_RESET="$(tput sgr0)"
  C_BOLD="$(tput bold)"
  C_GREEN="$(tput setaf 2)"
  C_YELLOW="$(tput setaf 3)"
  C_RED="$(tput setaf 1)"
  C_BLUE="$(tput setaf 4)"
else
  C_RESET=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
fi

step()  { printf "\n${C_BOLD}${C_BLUE}▸ %s${C_RESET}\n" "$*"; }
pass()  { printf "  ${C_GREEN}PASS${C_RESET} %s\n" "$*"; }
skip()  { printf "  ${C_YELLOW}SKIP${C_RESET} %s\n" "$*"; }
warn()  { printf "  ${C_YELLOW}WARN${C_RESET} %s\n" "$*"; }
fail()  { printf "  ${C_RED}FAIL${C_RESET} %s\n" "$*"; FAILED=1; }
info()  { printf "       %s\n" "$*"; }

# Interactive yes/no with a default. Returns 0 for yes, 1 for no.
# In non-interactive shells (no tty on stdin) returns the default.
ask_yes_no() {
  local prompt="$1" default="${2:-y}" reply
  if [ ! -t 0 ]; then
    [ "$default" = "y" ]; return
  fi
  local hint="[Y/n]"; [ "$default" = "n" ] && hint="[y/N]"
  printf "       %s %s " "$prompt" "$hint" >&2
  read -r reply || reply=""
  reply="${reply:-$default}"
  case "$reply" in
    [Yy]|[Yy][Ee][Ss]) return 0 ;;
    *) return 1 ;;
  esac
}

# Read a single secret from the user with input hidden (no echo). Returns
# empty when stdin is not a tty; caller decides what to do with empty.
ask_secret() {
  local prompt="$1" reply
  if [ ! -t 0 ]; then
    printf ""; return
  fi
  printf "       %s (input hidden): " "$prompt" >&2
  IFS= read -r -s reply || reply=""
  printf "\n" >&2
  printf "%s" "$reply"
}

# Read a single free-form value with an optional default. Returns the
# default when stdin is not a tty.
ask_input() {
  local prompt="$1" default="$2" reply
  if [ ! -t 0 ]; then
    printf "%s" "$default"; return
  fi
  if [ -n "$default" ]; then
    printf "       %s [%s]: " "$prompt" "$default" >&2
  else
    printf "       %s (blank to skip): " "$prompt" >&2
  fi
  read -r reply || reply=""
  printf "%s" "${reply:-$default}"
}

# Read current value of KEY from .env. Empty when missing or blank.
env_get() {
  local key="$1"
  [ -f .env ] || { printf ""; return; }
  awk -F= -v k="$key" '
    $0 ~ "^[[:space:]]*"k"=" {
      sub("^[[:space:]]*"k"=", "")
      # Strip surrounding single or double quotes.
      gsub(/^["\x27]|["\x27]$/, "")
      print
      exit
    }
  ' .env
}

# Upsert KEY=VALUE into .env (replaces existing line, appends if absent).
env_set() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { found = 0 }
    {
      if (substr($0, 1, length(k) + 1) == k "=") {
        print k "=" v; found = 1; next
      }
      print
    }
    END { if (!found) print k "=" v }
  ' .env > "$tmp"
  mv "$tmp" .env
}

add_path_entry() {
  local dir="$1"
  [ -n "$dir" ] && [ -d "$dir" ] || return 1
  case ":$PATH:" in
    *":$dir:"*) ;;
    *) PATH="$dir:$PATH"; export PATH ;;
  esac
}

windows_path_to_unix() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$p" 2>/dev/null || printf "%s" "$p"
  else
    printf "%s" "$p"
  fi
}

find_ffmpeg_bin() {
  local dir root found
  for dir in \
    /opt/homebrew/bin \
    /usr/local/bin \
    /opt/local/bin \
    /usr/bin \
    /snap/bin \
    "$HOME/.local/bin" \
    "$HOME/Tools/ffmpeg/bin" \
    /c/ffmpeg/bin
  do
    if { [ -x "$dir/ffmpeg" ] || [ -x "$dir/ffmpeg.exe" ]; } && \
       { [ -x "$dir/ffprobe" ] || [ -x "$dir/ffprobe.exe" ]; }; then
      printf "%s" "$dir"
      return 0
    fi
  done

  case "$OS" in
    MINGW*|MSYS*|CYGWIN*)
      for root in \
        "${LOCALAPPDATA:-}/Microsoft/WinGet/Packages" \
        "${USERPROFILE:-}/Tools" \
        "C:/ffmpeg" \
        "C:/Program Files" \
        "C:/Program Files (x86)"
      do
        [ -n "$root" ] || continue
        root="$(windows_path_to_unix "$root")"
        [ -d "$root" ] || continue
        found="$(find "$root" -type f -name ffmpeg.exe -print -quit 2>/dev/null || true)"
        [ -n "$found" ] || continue
        dir="$(dirname "$found")"
        [ -x "$dir/ffprobe.exe" ] || continue
        printf "%s" "$dir"
        return 0
      done
      ;;
  esac

  return 1
}

# ---------- locate or clone repo ----------------------------------------
# A "protean repo" is a directory whose pyproject.toml declares name="protean".
# When the script lives inside such a repo (normal case: user already cloned),
# we use that as the root. Otherwise (e.g. user downloaded setup.sh standalone
# or piped it through `curl | bash`), we offer to git-clone into ~/.protean.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FAILED=0
OS="$(uname -s)"

is_protean_repo() {
  [ -f "$1/pyproject.toml" ] && \
    grep -Eq '^[[:space:]]*name[[:space:]]*=[[:space:]]*"protean"' "$1/pyproject.toml" 2>/dev/null
}

CANDIDATE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if is_protean_repo "$CANDIDATE_ROOT"; then
  REPO_ROOT="$CANDIDATE_ROOT"
  printf "${C_BOLD}Protean setup${C_RESET} (root: %s)\n" "$REPO_ROOT"
else
  printf "${C_BOLD}Protean setup${C_RESET} (no local repo detected)\n"
  step "Clone Protean repository"
  if ! command -v git >/dev/null 2>&1; then
    fail "git not found — install git and re-run"
    exit 1
  fi
  default_dir="$HOME/.protean"
  url="${PROTEAN_REPO_URL:-https://github.com/AIBrain-Mnemis/Protean.git}"
  info "Source: $url"
  target="$(ask_input "Clone destination" "$default_dir")"
  if [ -d "$target/.git" ]; then
    pass "$target already cloned, reusing"
  elif [ -d "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; then
    fail "$target exists and is not empty — pick another path"
    exit 1
  else
    if git clone "$url" "$target"; then
      pass "cloned into $target"
    else
      fail "git clone failed"
      exit 1
    fi
  fi
  REPO_ROOT="$target"
fi

cd "$REPO_ROOT"

# ---------- uv -----------------------------------------------------------
step "Check uv (Python package manager)"
if command -v uv >/dev/null 2>&1; then
  pass "uv $(uv --version | awk '{print $2}')"
else
  warn "uv not found"
  if ask_yes_no "Install uv now via the official installer?" y; then
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
      # The installer typically writes to ~/.local/bin or ~/.cargo/bin; PATH
      # may not pick it up until the next shell. Probe both common locations.
      for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        [ -x "$cand" ] && PATH="$(dirname "$cand"):$PATH" && export PATH
      done
      if command -v uv >/dev/null 2>&1; then
        pass "uv $(uv --version | awk '{print $2}') (installed)"
        warn "Add uv's bin dir to your shell rc so future sessions see it"
      else
        fail "uv installed but not on PATH — restart your shell and re-run setup"
      fi
    else
      fail "uv installer failed"
    fi
  else
    fail "uv required; install manually: curl -LsSf https://astral.sh/uv/install.sh | sh"
  fi
fi

# ---------- python deps --------------------------------------------------
step "Sync Python dependencies (uv sync)"
if command -v uv >/dev/null 2>&1; then
  if uv sync; then pass "uv sync"; else fail "uv sync failed"; fi
else
  skip "uv missing (see previous step)"
fi

# ---------- git hooks ----------------------------------------------------
step "Configure Git hooks"
if [ ! -d .git ]; then
  skip "not a Git checkout"
else
  current_hooks="$(git config --local --get core.hooksPath 2>/dev/null || true)"
  if [ -n "$current_hooks" ] && [ "$current_hooks" != ".githooks" ]; then
    warn "core.hooksPath already set to $current_hooks; left unchanged"
  elif git config --local core.hooksPath .githooks; then
    pass "core.hooksPath=.githooks"
  else
    fail "could not configure core.hooksPath"
  fi
fi

# ---------- protean shim -------------------------------------------------
# Install ~/.local/bin/protean so users can run `protean ...` from anywhere
# instead of `uv --directory ~/.protean run protean ...`.
step "Install 'protean' command shim"
SHIM_DIR="$HOME/.local/bin"
SHIM_PATH="$SHIM_DIR/protean"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_PATH" <<EOF
#!/usr/bin/env bash
exec uv --directory "$REPO_ROOT" run protean "\$@"
EOF
chmod +x "$SHIM_PATH"
pass "installed $SHIM_PATH → protean (at $REPO_ROOT)"
case ":$PATH:" in
  *":$SHIM_DIR:"*) ;;
  *) warn "$SHIM_DIR is not on your PATH — add it to your shell rc" ;;
esac

# ---------- .env ---------------------------------------------------------
step "Bootstrap .env"
if [ -f .env ]; then
  pass ".env already present"
elif [ -f .env.example ]; then
  cp .env.example .env
  pass "created .env from .env.example"
else
  fail ".env.example missing — cannot create .env"
fi

# ---------- storage paths ------------------------------------------------
step "Configure Protean storage"
current_data="$(env_get PROTEAN_DATA_DIR)"
current_skills="$(env_get PROTEAN_SKILLS_DIR)"
current_recordings="$(env_get PROTEAN_RECORDINGS_DIR)"
configured_count=0
[ -n "$current_data" ] && configured_count=$((configured_count + 1))
[ -n "$current_skills" ] && configured_count=$((configured_count + 1))
[ -n "$current_recordings" ] && configured_count=$((configured_count + 1))
if [ "$configured_count" -ne 0 ] && [ "$configured_count" -ne 3 ]; then
  warn "partial custom storage configuration detected; existing paths were preserved"
elif ! command -v uv >/dev/null 2>&1; then
  skip "uv missing — cannot configure storage"
else
  default_data="$REPO_ROOT/data"
  suggested_data="${current_data:-$default_data}"
  selected_data="$(ask_input "Protean data directory" "$suggested_data")"
  if [ -n "$current_data" ] && [ "$selected_data" = "$current_data" ]; then
    pass "storage paths unchanged"
    info "Data: $current_data"
  else
    case "$selected_data" in
      /*) data_root="$selected_data" ;;
      *)  data_root="$REPO_ROOT/$selected_data" ;;
    esac
    uv run python scripts/configure_storage.py \
        --repo-root "$REPO_ROOT" \
        --env-file "$REPO_ROOT/.env" \
        --data-dir "$data_root" \
        --prompt-migration
    storage_status=$?
    case "$storage_status" in
      0) pass "storage configured at $data_root" ;;
      2) skip "storage paths unchanged" ;;
      *) fail "storage migration failed" ;;
    esac
  fi
fi

# ---------- ffmpeg -------------------------------------------------------
step "Check ffmpeg (provides ffmpeg + ffprobe)"
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  ffmpeg_bin="$(find_ffmpeg_bin || true)"
  if [ -n "$ffmpeg_bin" ]; then
    add_path_entry "$ffmpeg_bin"
    info "found ffmpeg tools at $ffmpeg_bin"
  fi
fi

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  pass "ffmpeg + ffprobe present"
else
  fail "ffmpeg / ffprobe missing"
  case "$OS" in
    Darwin)  info "Install: brew install ffmpeg" ;;
    Linux)   info "Install: sudo apt install ffmpeg  (or distro equivalent)" ;;
    MINGW*|MSYS*|CYGWIN*) info "Install: winget install Gyan.FFmpeg  (or add its bin directory to PATH)" ;;
    *)       info "Install ffmpeg for your platform" ;;
  esac
fi

# ---------- node ---------------------------------------------------------
step "Check Node.js (for electron-bridge / realtime path)"
NODE_OK=0
if command -v node >/dev/null 2>&1; then
  NODE_VER="$(node -v | sed 's/^v//')"
  NODE_MAJOR="${NODE_VER%%.*}"
  if [ "$NODE_MAJOR" -ge 22 ] 2>/dev/null; then
    pass "node v$NODE_VER (>= 22, realtime path supported)"
    NODE_OK=1
  elif [ "$NODE_MAJOR" -ge 18 ] 2>/dev/null; then
    warn "node v$NODE_VER (>= 18, CUA terminal tool only — realtime needs >= 22)"
  else
    fail "node v$NODE_VER too old (need >= 22 for realtime, >= 18 for CUA terminal)"
  fi
else
  warn "node not found — needed only for realtime voice path"
  info "Install Node 22+ (https://nodejs.org) or set PROTEAN_CUA_TERMINAL=false to skip"
fi

# ---------- electron-bridge build ----------------------------------------
step "Build electron-bridge (realtime voice / screen-share)"
build_bridge() {
  (
    cd electron-bridge
    if [ ! -d node_modules ]; then
      info "npm install …"
      npm install --silent || exit 1
    else
      info "node_modules present, skipping npm install"
    fi
    info "npm run build …"
    npm run build --silent
  )
}

if [ ! -d electron-bridge ]; then
  skip "electron-bridge/ not present"
elif [ "$NODE_OK" != "1" ]; then
  skip "Node >= 22 required (see previous step)"
else
  info "Required only for the realtime voice / screen-share daemon."
  if ask_yes_no "Build electron-bridge now?" y; then
    if build_bridge; then pass "electron-bridge built"; else fail "electron-bridge build failed"; fi
  else
    skip "user declined"
  fi
fi

# ---------- agents wiring ------------------------------------------------
step "Wire external agent runtimes (Codex / Claude Code)"
wire_agent() {
  local agent="$1"
  info "agents setup $agent …"
  if .venv/bin/protean agents setup "$agent"; then
    pass "wired $agent"
  else
    fail "agents setup $agent failed"
  fi
}

if ! command -v uv >/dev/null 2>&1; then
  skip "uv missing — cannot run 'protean agents setup'"
elif [ ! -x .venv/bin/protean ]; then
  skip ".venv/bin/protean missing — uv sync failed?"
else
  # interactive: ask once per known agent if its home directory exists
  info "Skips an agent automatically when its home directory is absent."
  for kind in codex claude_code; do
    case "$kind" in
      codex)        home="${CODEX_HOME:-$HOME/.codex}";  label="Codex" ;;
      claude_code)  home="${CLAUDE_HOME:-$HOME/.claude}"; label="Claude Code" ;;
    esac
    if [ ! -d "$home" ]; then
      skip "$label not installed ($home missing)"
      continue
    fi
    if ask_yes_no "Wire $label ($home)?" y; then
      wire_agent "$kind"
    else
      skip "$label declined"
    fi
  done
fi

# ---------- .env: default LLM provider (interactive) --------------------
# Maps provider id → label, key var, and code-default model.
_provider_key()   { case "$1" in openai) echo OPENAI_API_KEY ;; anthropic) echo ANTHROPIC_API_KEY ;; gemini) echo GEMINI_API_KEY ;; doubao) echo ARK_API_KEY ;; esac; }
_provider_model() { case "$1" in openai) echo OPENAI_MODEL ;; anthropic) echo ANTHROPIC_MODEL ;; gemini) echo GEMINI_MODEL ;; doubao) echo ARK_MODEL ;; esac; }
_provider_default_model() { case "$1" in openai) echo gpt-5 ;; anthropic) echo claude-sonnet-4-5 ;; gemini) echo gemini-3.5-flash ;; doubao) echo doubao-pro-256k ;; esac; }

step "Configure default LLM provider in .env"
current_provider="$(env_get PROTEAN_DEFAULT_PROVIDER)"
if [ -n "$current_provider" ]; then
  info "Current: PROTEAN_DEFAULT_PROVIDER=$current_provider"
  if ask_yes_no "Change the default provider?" n; then do_configure=1; else do_configure=0; skip "kept existing"; fi
else
  if ask_yes_no "Configure the default provider now?" y; then do_configure=1; else do_configure=0; skip "default provider (declined)"; fi
fi
if [ "$do_configure" = "1" ]; then
  case "$current_provider" in
    openai)    cur_idx=1 ;;
    anthropic) cur_idx=2 ;;
    gemini)    cur_idx=3 ;;
    doubao)    cur_idx=4 ;;
    *)         cur_idx=1 ;;
  esac
  info "Options: 1) openai   2) anthropic   3) gemini   4) doubao"
  choice="$(ask_input "Pick the default provider [1-4]" "$cur_idx")"
  case "$choice" in
    1|openai)    provider=openai ;;
    2|anthropic) provider=anthropic ;;
    3|gemini)    provider=gemini ;;
    4|doubao)    provider=doubao ;;
    *) fail "unknown choice: $choice"; provider="" ;;
  esac
  if [ -n "$provider" ]; then
    key_var="$(_provider_key "$provider")"
    model_var="$(_provider_model "$provider")"
    default_model="$(_provider_default_model "$provider")"
    current_key="$(env_get "$key_var")"
    current_model="$(env_get "$model_var")"
    if [ -n "$current_key" ]; then
      secret="$(ask_secret "$provider API key (blank to keep existing)")"
    else
      secret="$(ask_secret "$provider API key")"
    fi
    if [ -z "$secret" ] && [ -z "$current_key" ]; then
      fail "$key_var (empty input) — default provider not written"
    else
      env_set PROTEAN_DEFAULT_PROVIDER "$provider"
      if [ -n "$secret" ]; then
        env_set "$key_var" "$secret"
        pass "PROTEAN_DEFAULT_PROVIDER=$provider, $key_var updated"
      else
        pass "PROTEAN_DEFAULT_PROVIDER=$provider, $key_var kept"
      fi
      model_default="${current_model:-$default_model}"
      model="$(ask_input "$provider model" "$model_default")"
      env_set "$model_var" "$model"
      pass "$model_var=$model"
    fi
  fi
fi

# ---------- .env: realtime voice daemon (optional) ----------------------
step "Configure realtime voice daemon in .env (optional)"
current_realtime="$(env_get PROTEAN_REALTIME_MODEL)"
if [ -n "$current_realtime" ]; then
  info "Current: PROTEAN_REALTIME_MODEL=$current_realtime"
  if ask_yes_no "Change realtime configuration?" n; then do_configure=1; else do_configure=0; skip "kept existing"; fi
else
  info "Realtime voice / screen-share runs through Gemini Live."
  if ask_yes_no "Enable the realtime daemon now?" n; then do_configure=1; else do_configure=0; skip "realtime (declined)"; fi
fi
if [ "$do_configure" = "1" ]; then
  current_gemini="$(env_get GEMINI_API_KEY)"
  if [ -n "$current_gemini" ]; then
    gemini_key="$(ask_secret "Gemini API key (blank to keep existing)")"
    if [ -n "$gemini_key" ]; then
      env_set GEMINI_API_KEY "$gemini_key"
      pass "GEMINI_API_KEY updated"
    else
      pass "GEMINI_API_KEY kept"
    fi
  else
    gemini_key="$(ask_secret "Gemini API key")"
    if [ -z "$gemini_key" ]; then
      fail "GEMINI_API_KEY (empty input) — realtime not enabled"
    else
      env_set GEMINI_API_KEY "$gemini_key"
      pass "GEMINI_API_KEY set"
    fi
  fi
  if [ -n "$(env_get GEMINI_API_KEY)" ]; then
    default_realtime="gemini-3.1-flash-live-preview"
    model_default="${current_realtime:-$default_realtime}"
    model="$(ask_input "Realtime model" "$model_default")"
    env_set PROTEAN_REALTIME_MODEL "$model"
    pass "PROTEAN_REALTIME_MODEL=$model"
  fi
fi

# ---------- .env: speech transcription (optional) -----------------------
step "Configure speech transcription in .env (optional)"
current_asr_url="$(env_get PROTEAN_ASR_URL)"
if [ -n "$current_asr_url" ]; then
  info "Current: PROTEAN_ASR_URL=$current_asr_url"
  info "         PROTEAN_ASR_MODEL=$(env_get PROTEAN_ASR_MODEL)"
  if ask_yes_no "Change transcription configuration?" n; then do_configure=1; else do_configure=0; skip "kept existing"; fi
else
  info "Transcribes mic audio captured during recording."
  info "Needs an OpenAI-compatible /v1/audio/transcriptions endpoint."
  if ask_yes_no "Enable speech transcription now?" n; then do_configure=1; else do_configure=0; skip "speech transcription (declined)"; fi
fi
if [ "$do_configure" = "1" ]; then
  asr_url="$(ask_input "ASR endpoint URL" "$current_asr_url")"
  if [ -z "$asr_url" ]; then
    fail "PROTEAN_ASR_URL (empty input) — transcription not enabled"
  else
    env_set PROTEAN_ASR_URL "$asr_url"
    pass "PROTEAN_ASR_URL=$asr_url"
    current_asr_model="$(env_get PROTEAN_ASR_MODEL)"
    asr_model="$(ask_input "ASR model" "${current_asr_model:-whisper-1}")"
    env_set PROTEAN_ASR_MODEL "$asr_model"
    pass "PROTEAN_ASR_MODEL=$asr_model"
    current_asr_key="$(env_get PROTEAN_ASR_API_KEY)"
    if [ -n "$current_asr_key" ]; then
      asr_key="$(ask_secret "ASR API key (blank to keep existing)")"
      if [ -n "$asr_key" ]; then
        env_set PROTEAN_ASR_API_KEY "$asr_key"
        pass "PROTEAN_ASR_API_KEY updated"
      else
        pass "PROTEAN_ASR_API_KEY kept"
      fi
    else
      asr_key="$(ask_secret "ASR API key (blank for self-hosted)")"
      if [ -n "$asr_key" ]; then
        env_set PROTEAN_ASR_API_KEY "$asr_key"
        pass "PROTEAN_ASR_API_KEY set"
      else
        skip "PROTEAN_ASR_API_KEY (blank — fine for self-hosted)"
      fi
    fi
  fi
fi

# ---------- platform-specific permissions reminder -----------------------
if [ "$OS" = "Darwin" ]; then
  step "macOS permissions (manual)"
  info "Grant your terminal both permissions in System Settings:"
  info "  • Privacy & Security → Accessibility"
  info "  • Privacy & Security → Screen Recording"
  info "(setup script cannot enable these for you)"
fi

# ---------- summary ------------------------------------------------------
echo
if [ "$FAILED" = "0" ]; then
  printf "${C_BOLD}${C_GREEN}✓ Setup complete.${C_RESET}\n"
  printf "Next: %sprotean --help%s\n" "$C_BOLD" "$C_RESET"
  exit 0
else
  printf "${C_BOLD}${C_RED}✗ Setup finished with failures.${C_RESET} Address the FAIL items above and re-run.\n"
  exit 1
fi
