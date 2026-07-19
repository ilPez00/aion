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
  daily)
    # Daily-driver boot: ensure the physis_pro coherence engine is running,
    # then launch the aion cockpit. Safe to re-run (no-op if already up).
    ensure_venv
    PHYSIS_BIN="${PHYSIS_PRO_BIN:-$HOME/physis_pro/target/debug/physis-pro-web}"
    if command -v curl >/dev/null 2>&1 && \
       curl -sS -m 3 http://127.0.0.1:19876/api/v1/embedder >/dev/null 2>&1; then
      echo "[aion] physis engine already live (:19876)"
    elif [ -x "$PHYSIS_BIN" ]; then
      echo "[aion] starting physis engine (:19876)..."
      PHYSIS_PORT=19876 nohup "$PHYSIS_BIN" >/tmp/physis_web.log 2>&1 &
      sleep 3
    else
      echo "[aion] WARNING: physis engine not found at $PHYSIS_BIN — aion boots without coherence brain (Physis workspace will show 'offline')."
    fi
    exec "$PY" -m aion.ui.app
    ;;
  desktop)
    # Emit a .desktop file so aion shows in the app menu + can autostart.
    DEST="$HOME/.local/share/applications/aion.desktop"
    mkdir -p "$(dirname "$DEST")"
    cat > "$DEST" <<EOF
[Desktop Entry]
Name=aion Cockpit
Comment=Multi-harness AI cockpit + physis coherence brain
Exec=$(pwd)/aion.sh daily
Icon=utilities-terminal
Terminal=true
Type=Application
Categories=Development;Utility;
EOF
    echo "[aion] wrote $DEST"
    echo "[aion] to autostart on login, symlink it:"
    echo "        ln -s $DEST \$HOME/.config/autostart/aion.desktop"
    ;;
  *)
    echo "usage: ./aion.sh [run|daily|desktop|install|web|test|shell]" >&2
    exit 1
    ;;
esac
