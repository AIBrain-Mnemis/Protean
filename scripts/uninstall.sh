#!/usr/bin/env bash
# All-in-one teardown for Protean. The inverse of scripts/setup.sh.
#
# Non-interactive: removes everything setup.sh creates, in order.
# Shared tools (uv, ffmpeg, node) are never touched.

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

step() { printf "\n${C_BOLD}${C_BLUE}▸ %s${C_RESET}\n" "$*"; }
pass() { printf "  ${C_GREEN}PASS${C_RESET} %s\n" "$*"; }
skip() { printf "  ${C_YELLOW}SKIP${C_RESET} %s\n" "$*"; }
fail() { printf "  ${C_RED}FAIL${C_RESET} %s\n" "$*"; FAILED=1; }

# ---------- locate repo --------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
printf "${C_BOLD}Protean uninstall${C_RESET} (root: %s)\n" "$REPO_ROOT"
FAILED=0

# ---------- 1. unwire external agents -----------------------------------
step "Unwire external agent runtimes (Codex / Claude Code)"
if [ ! -x .venv/bin/protean ]; then
  skip ".venv/bin/protean missing — nothing to unwire"
elif .venv/bin/protean agents uninstall all; then
  pass "agents uninstall all"
else
  fail "agents uninstall all failed"
fi

# ---------- 2. protean command shim --------------------------------------
step "Remove 'protean' command shim"
SHIM_PATH="$HOME/.local/bin/protean"
if [ -f "$SHIM_PATH" ]; then
  rm -f "$SHIM_PATH" && pass "removed $SHIM_PATH"
else
  skip "$SHIM_PATH not present"
fi

# ---------- 3. electron-bridge build artifacts --------------------------
step "Remove electron-bridge build artifacts"
if [ ! -d electron-bridge ]; then
  skip "electron-bridge/ not present"
else
  for d in electron-bridge/node_modules electron-bridge/dist; do
    if [ -d "$d" ]; then
      rm -rf "$d" && pass "removed $d"
    fi
  done
fi

# ---------- 3. .venv ----------------------------------------------------
step "Remove Python virtual environment (.venv)"
if [ -d .venv ]; then
  rm -rf .venv && pass "removed .venv"
else
  skip ".venv not present"
fi

# ---------- 4. .env -----------------------------------------------------
step "Remove .env (provider keys + model overrides)"
if [ -f .env ]; then
  rm -f .env && pass "removed .env"
else
  skip ".env not present"
fi

# ---------- 5. ~/.protean clone (only if we live there) -----------------
if [ "$REPO_ROOT" = "$HOME/.protean" ]; then
  step "Remove cloned repository ($REPO_ROOT)"
  # Schedule self-deletion via a detached shell so we can rm our own cwd.
  (sleep 1; rm -rf "$REPO_ROOT") &
  pass "scheduled removal of $REPO_ROOT"
fi

# ---------- summary -----------------------------------------------------
echo
if [ "$FAILED" = "0" ]; then
  printf "${C_BOLD}${C_GREEN}✓ Uninstall complete.${C_RESET}\n"
  printf "Shared tools (uv, ffmpeg, node) were left alone.\n"
  exit 0
else
  printf "${C_BOLD}${C_RED}✗ Uninstall finished with failures.${C_RESET} See FAIL items above.\n"
  exit 1
fi
