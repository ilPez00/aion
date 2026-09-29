#!/usr/bin/bash
# hud_control.sh — unified HUD launcher and control script.
#
# Usage:
#   ./hud_control.sh tui       # start the TUI cockpit
#   ./hud_control.sh web        # start the web HUD (port 8742)
#   ./hud_control.sh node       # headless instance for fleet peers
#   ./hud_control.sh status     # check what's running
#   ./hud_control.sh help       # this message

cd "$(dirname "$0")"

cd "/home/gio/dev/aion"

VENV=.venv
PY="$VENV/bin/python"

# Check if virtual environment exists
if [ ! -x "$PY" ]; then
    echo "[aion] setting up environment..."
    if command -v uv >/dev/null 2>&1; then
        uv venv "$VENV" >/dev/null
    else
        python3 -m venv "$VENV" >/dev/null
    fi
    . "$VENV/bin/activate"
    pip install -e ".[voice]" >/dev/null 2>&1 || true
fi

# Add to PATH so scripts work
source "$VENV/bin/activate"

case "$1" in
    tui|tui-cockpit)
        echo "[aion] starting TUI cockpit..."
        $PY -m aion.ui.app
        ;;

    web|web-hud)
        echo "[aion] starting web HUD on http://127.0.0.1:8742..."
        $PY scripts/aion_web.py
        ;;

    node|aion-node)
        echo "[aion] starting headless node on port 8765..."
        $PY scripts/aion_node.py
        ;;

    status|check)
        echo "[aion] checking running services..."
        if pgrep -f "aion_web.py" >/dev/null; then
            echo "  web HUD:    http://127.0.0.1:8742"
        else
            echo "  web HUD:    not running"
        fi
        if pgrep -f "aion_node.py" >/dev/null; then
            echo "  headless node: listening on port 8765"
        else
            echo "  headless node: not running"
        fi
        if pgrep -f "aion.ui.app" >/dev/null; then
            echo "  TUI cockpit: running"
        else
            echo "  TUI cockpit: not running"
        fi
        ;;

    help|--help|-h)
        echo "Usage: ./hud_control.sh {tui|web|node|status|help}"
        echo ""
        echo "Commands:"
        echo "  tui       start the TUI cockpit (Text-based UI)"
        echo "  web       start the web HUD (http://127.0.0.1:8742)"
        echo "  node      start headless instance for fleet peers"
        echo "  status    check what's currently running"
        echo "  help      show this message"
        echo ""
        echo "Notes:"
        echo "  The web HUD provides a browser-based interface with"
        echo "  enhanced capabilities including voice control via"
        echo "  Web Speech API and PTY access for terminal sessions."
        echo ""
        echo "  The TUI is the original text-mode interface, optimized"
        echo "  for performance and keyboard-driven workflows."
        echo ""
        echo "  Headless nodes are machine-controlled instances that can"
        echo "  receive work from fleet commands without direct interaction."
        ;;

    *)
        echo "Unknown command: $1"
        echo "Run '$0 help' for usage information."
        exit 1
        ;;
esac