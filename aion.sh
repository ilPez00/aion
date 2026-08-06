#!/usr/bin/env bash
# aion.sh — one-command launcher for the aion Iron Man cockpit.
#
# Run `./aion.sh help` for the full command list.
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

# Open a URL in Chrome specifically.
#
# The HUD's voice control is the Web Speech API, which only Chrome and Edge
# implement — xdg-open honours the desktop default, so on a Firefox box the
# HUD would come up with its microphone permanently broken and no clue why.
# Chromium counts; Firefox does not, and we say so rather than opening it and
# letting voice mysteriously do nothing.
open_in_chrome() {
  url="$1"
  for b in google-chrome google-chrome-stable chromium chromium-browser \
           brave-browser microsoft-edge vivaldi-stable; do
    if command -v "$b" >/dev/null 2>&1; then
      echo "[aion] opening in $b"
      setsid nohup "$b" --new-window "$url" >/dev/null 2>&1 </dev/null &
      return 0
    fi
  done
  if [ -d "/Applications/Google Chrome.app" ]; then
    open -a "Google Chrome" "$url"; return 0
  fi
  echo "[aion] no Chrome-family browser found — voice control needs one" >&2
  echo "[aion] opening your default browser instead; everything else works" >&2
  command -v xdg-open >/dev/null 2>&1 && xdg-open "$url" >/dev/null 2>&1 || true
  return 1
}

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
  update)
    # What is every machine in the fleet running? Reports drift; only pulls
    # from origin, and only when asked.
    ensure_venv
    shift || true
    exec "$PY" scripts/aion_update.py "$@"
    ;;
  node)
    # A machine that can be GIVEN work with nobody sitting at it. The remote
    # listener otherwise only exists while the Textual cockpit is open, which
    # is exactly backwards for the machines you dispatch to.
    ensure_venv
    shift || true
    exec "$PY" scripts/aion_node.py "$@"
    ;;
  up)
    # The "just start everything" command. Physis brain if it's there, the web
    # HUD, and a browser pointed at it. Everything else is opt-in flags.
    ensure_venv
    shift || true
    OPEN=1; COCKPIT=0
    for a in "$@"; do
      case "$a" in
        --no-open) OPEN=0 ;;
        --cockpit) COCKPIT=1 ;;
        *) echo "[aion] unknown flag: $a (try --no-open, --cockpit)" >&2; exit 1 ;;
      esac
    done

    PHYSIS_BIN="${PHYSIS_PRO_BIN:-$HOME/physis_pro/target/debug/physis-pro-web}"
    if command -v curl >/dev/null 2>&1 && \
       curl -sS -m 2 http://127.0.0.1:19876/api/v1/embedder >/dev/null 2>&1; then
      echo "[aion] physis brain: live"
    elif [ -x "$PHYSIS_BIN" ]; then
      echo "[aion] physis brain: starting"
      PHYSIS_PORT=19876 nohup "$PHYSIS_BIN" >/tmp/physis_web.log 2>&1 &
    else
      echo "[aion] physis brain: not installed (coherence panels stay offline)"
    fi

    PORT="${AION_WEB_PORT:-8742}"
    if curl -sS -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
      echo "[aion] web HUD already up on :$PORT"
    else
      echo "[aion] web HUD: starting on :$PORT"
      # setsid so the HUD outlives this shell; the log is where to look first
      setsid nohup "$PY" scripts/aion_web.py >/tmp/aion_web.log 2>&1 </dev/null &
      for _ in $(seq 1 40); do
        curl -sS -m 1 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
        sleep 0.25
      done
    fi

    if ! curl -sS -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
      echo "[aion] web HUD failed to start — see /tmp/aion_web.log" >&2
      tail -5 /tmp/aion_web.log >&2 2>/dev/null || true
      exit 1
    fi
    echo "[aion] ready → http://127.0.0.1:$PORT"

    if [ "$OPEN" -eq 1 ]; then
      open_in_chrome "http://127.0.0.1:$PORT/" || true
    fi
    if [ "$COCKPIT" -eq 1 ]; then
      echo "[aion] launching the TUI cockpit (Ctrl-C leaves the HUD running)"
      exec "$PY" -m aion.ui.app
    fi
    echo "[aion] stop it with: ./aion.sh down"
    ;;
  down)
    # Stop what `up` started. Never touches a cockpit you are sitting in.
    if pkill -f "scripts/aion_web.py" 2>/dev/null; then
      echo "[aion] web HUD stopped"
    else
      echo "[aion] web HUD was not running"
    fi
    ;;
  status)
    PORT="${AION_WEB_PORT:-8742}"
    if curl -sS -m 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
      echo "[aion] web HUD  : up   → http://127.0.0.1:$PORT"
    else
      echo "[aion] web HUD  : down"
    fi
    if curl -sS -m 2 http://127.0.0.1:19876/api/v1/embedder >/dev/null 2>&1; then
      echo "[aion] physis    : up"
    else
      echo "[aion] physis    : down"
    fi
    ls -1 "$HOME/.aion/instances" 2>/dev/null | while read -r i; do
      echo "[aion] cockpit   : $i"
    done
    ;;
  graph)
    # Open the graph file manager straight at a directory (default: cwd).
    # Saves the "which module, which path, which port" dance every time you
    # actually want the thing: `./aion.sh graph ~/dev/foo` and it is on screen.
    ensure_venv
    shift || true
    TARGET="${1:-$PWD}"
    if [ ! -d "$TARGET" ]; then
      echo "[aion] not a directory: $TARGET" >&2; exit 1
    fi
    TARGET="$(cd "$TARGET" && pwd)"
    export AION_FS_DIR="$TARGET"
    # Widen the sandbox only as far as the target actually needs.
    if [ -z "${AION_FS_ROOT:-}" ]; then
      case "$TARGET" in
        "$HOME"/*|"$HOME") : ;;                 # inside $HOME: default root is fine
        *) export AION_FS_ROOT="$TARGET" ;;     # outside: pin the root to it
      esac
    fi
    URL="http://127.0.0.1:${AION_WEB_PORT:-8742}/"
    echo "[aion] graph file manager → $TARGET"
    ( sleep 2; open_in_chrome "$URL" >/dev/null 2>&1 || true ) &
    exec "$PY" scripts/aion_web.py
    ;;
  peers)
    # Machines this cockpit can reach over SSH. See docs/ssh-peers.md.
    ensure_venv
    shift || true
    exec "$PY" scripts/aion_peers.py "$@"
    ;;
  doctor)
    # Answers "why isn't X working" without reading three docs first.
    ensure_venv
    echo "[aion] environment"
    "$PY" - <<'PYEOF'
import importlib, os, shutil, socket, sys
sys.path.insert(0, "src")

def line(ok, name, note=""):
    print(f"  [{'ok' if ok else '--'}] {name:<22} {note}")

print(f"  python {sys.version.split()[0]}  ({sys.executable})")
for mod, why in [("textual", "TUI cockpit"), ("psutil", "system HUD"),
                 ("websockets", "web HUD terminal"), ("pyte", "PTY screen"),
                 ("requests", "web module"), ("faster_whisper", "offline voice"),
                 ("evdev", "joystick"), ("bleak", "Colmi ring"),
                 ("serial", "CyclUno deck")]:
    try:
        importlib.import_module(mod); line(True, mod, why)
    except Exception:
        line(False, mod, f"{why} — pip install -e '.[web,voice]'")

print("[aion] services")
def probe(host, port, name):
    s = socket.socket(); s.settimeout(0.4)
    ok = s.connect_ex((host, port)) == 0
    s.close(); line(ok, name, f"{host}:{port}")
    return ok
probe("127.0.0.1", 19876, "physis brain")
probe("127.0.0.1", 19280, "FCM llm proxy")
probe("127.0.0.1", int(os.environ.get("AION_WEB_PORT", 8742)), "web HUD")

print("[aion] paths")
from aion import fsgraph
line(True, "fs scan root", str(fsgraph.scan_root()))
line(os.path.exists(os.path.expanduser("~/.aion/token")), "fleet token",
     "~/.aion/token — needed for LAN access")
line(bool(shutil.which("latexmk")), "latexmk", "LaTeX module")
line(bool(os.environ.get("GROQ_API_KEY")), "GROQ_API_KEY", "llm fallback")
PYEOF
    ;;
  help|-h|--help)
    cat <<'EOF'
aion — splitscreen HUD + application desktop

  ./aion.sh up              START EVERYTHING: physis + web HUD + browser
  ./aion.sh down            stop the web HUD
  ./aion.sh status          what is running right now

  ./aion.sh                 launch the TUI cockpit
  ./aion.sh daily           start the physis brain, then the cockpit
  ./aion.sh web             serve the web HUD    → http://127.0.0.1:8742
  ./aion.sh graph [DIR]     web HUD opened on the graph file manager for DIR
  ./aion.sh peers           machines reachable over SSH (list/add/rm/test)
  ./aion.sh doctor          check deps, services, paths — run this first when stuck
  ./aion.sh desktop         install a .desktop launcher
  ./aion.sh install         create/refresh .venv + deps
  ./aion.sh test            run the test suite
  ./aion.sh shell [ARGS]    run the venv python

  ./aion.sh up --cockpit    also drop into the TUI once the HUD is up
  ./aion.sh up --no-open    do not launch a browser

Web HUD modules (keys 1-9)
  Desk · Files · Agents · Repos · Vault · System · Board · Term · Chat ·
  LaTeX · Settings
  ctrl-K   search everything    g / l  graph <-> list    / filter nodes
  r        rescan               0      fit graph         ? inspector

Environment
  AION_WEB_HOST=0.0.0.0     expose on the LAN (token-gated, see README)
  AION_WEB_PORT=8742        HTTP port
  AION_FS_ROOT=~            sandbox for the graph file manager
  AION_FS_DIR=.             directory the graph file manager opens on
  AION_FS_MAX_FILES=600     scan cap
EOF
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
    echo "aion: unknown command '${1}'. Try: ./aion.sh help" >&2
    exit 1
    ;;
esac
