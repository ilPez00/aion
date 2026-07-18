#!/usr/bin/env bash
# aion.sh — one-command launcher for the aion Iron Man cockpit.
#
# Usage:
#   ./aion.sh            # run the cockpit (boots venv, installs deps if missing)
#   ./aion.sh install    # create/refresh .venv + install deps (editable)
#   ./aion.sh web        # run the web HUD server instead of the TUI
#   ./aion.sh test       # run the test suite
#   ./aion.sh shell      # drop into the venv shell
#
# Behaviour:
#   - prefers uv if available (fast), else falls back to python -m venv.
#   - uses the repo-local .venv; never touches a global interpreter.
#   - on first run (or if deps missing) it auto-installs so `./aion.sh` just works.
set -euo pipefail

cd "$(dirname "$0")"

VENV=.venv
PY="$VENV/bin/python"
HAS_UV=0
command -v uv >/dev/null 2>&1 && HAS_UV=1

ensure_venv() {
  if [ ! -x "$PY" ]; then
    echo "[aion] creating virtualenv (.venv)..."
    if [ "$HAS_UV" -eq 1 ]; then
      uv venv "$VENV" >/dev/null
    else
      python3 -m venv "$VENV"
    fi
  fi
  # install deps if import fails (cheap check)
  if ! "$PY" -c "import aion, textual, psutil" >/dev/null 2>&1; then
    echo "[aion] installing dependencies (editable)..."
    if [ "$HAS_UV" -eq 1 ]; then
      uv pip install -e . >/dev/null
    else
      "$PY" -m pip install --quiet -e .
    fi
  fi
}

case "${1:-run}" in
  install)
    ensure_venv
    echo "[aion] ready. run ./aion.sh to launch."
    ;;
  test)
    ensure_venv
    "$PY" -m pytest tests/ -q
    ;;
  web)
    ensure_venv
    shift || true
    exec "$PY" scripts/aion_web.py "$@"
    ;;
  shell)
    ensure_venv
    exec "$PY" "${@:2}"
    ;;
  run|"")
    ensure_venv
    exec "$PY" -m aion.ui.app
    ;;
  *)
    echo "usage: ./aion.sh [run|install|web|test|shell]" >&2
    exit 1
    ;;
esac
