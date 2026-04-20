#!/usr/bin/env bash
# start.sh — launch the nse-universe interactive CLI.
#
# Ensures: working directory is the repo root, uv is present, .venv exists
# and is in sync with pyproject.toml, then hands off to the menu.
#
# Safe to run from any cwd. Idempotent. First run is slow (installs deps);
# subsequent runs are near-instant.

set -euo pipefail

# ─── locate repo root (script's own directory) ───────────────────────
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ─── terminal colors (fall back to plain if not a tty) ───────────────
if [[ -t 1 ]]; then
  C_DIM=$'\e[2m'; C_OK=$'\e[32m'; C_WARN=$'\e[33m'; C_ERR=$'\e[31m'; C_RESET=$'\e[0m'
else
  C_DIM=''; C_OK=''; C_WARN=''; C_ERR=''; C_RESET=''
fi

log()  { printf '%s▸%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
ok()   { printf '%s✓%s %s\n' "$C_OK" "$C_RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$C_WARN" "$C_RESET" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; exit 1; }

# ─── check uv ────────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found on PATH"
  echo
  echo "Install it with one of:"
  echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "    brew install uv              # macOS"
  echo "    pipx install uv              # if you prefer pipx"
  echo
  die "re-run after installing uv"
fi
log "uv $(uv --version | awk '{print $2}')"

# ─── ensure venv is in sync with pyproject.toml + uv.lock ─────────────
# `uv sync` is fast when nothing changed (checks lockfile + venv hash),
# so run it unconditionally — cheaper than detecting staleness ourselves.
log "syncing dependencies …"
if ! uv sync --quiet 2>&1; then
  die "uv sync failed — inspect pyproject.toml / uv.lock"
fi
ok "venv ready at .venv/"

# ─── sanity: can we import the package? ──────────────────────────────
if ! uv run python -c "import nse_universe" 2>/dev/null; then
  die "package import failed — try: uv sync --reinstall"
fi

# ─── first-run hint if data dir is empty ─────────────────────────────
if [[ ! -d "$REPO_ROOT/data/raw" ]] || [[ -z "$(ls -A "$REPO_ROOT/data/raw" 2>/dev/null | grep -v '^_quarantine$' || true)" ]]; then
  warn "data/raw/ is empty — pick 'Sync bhavcopy' or 'Full pipeline' to fetch history"
fi

# ─── launch the menu ─────────────────────────────────────────────────
log "launching nse-universe menu …"
echo
exec uv run nse-universe "$@"
