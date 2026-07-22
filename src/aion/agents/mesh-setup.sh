#!/usr/bin/env bash
# mesh-setup.sh — install node discovery + cron on any machine
#
# Prefer running from a checked-out repo:  bash src/aion/agents/mesh-setup.sh
# Remote install requires a PINNED commit and the expected node.py hash — never
# pipe an unpinned `curl | bash` from a mutable branch (MITM => root RCE):
#   MESH_REF=<full-commit-sha> MESH_NODE_SHA256=<sha256-of-node.py> \
#     bash <(curl -fsSL https://raw.githubusercontent.com/ilPez00/aion/<sha>/src/aion/agents/mesh-setup.sh)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_PY="$SCRIPT_DIR/node.py"
CRON_LOG="$HOME/.cache/aion-mesh/cron.log"
NODE_SCRIPT="/usr/local/bin/aion-mesh-scan"
MESH_REF="${MESH_REF:-}"                 # commit SHA to pin the remote download
MESH_NODE_SHA256="${MESH_NODE_SHA256:-}" # expected sha256 of node.py

echo "=== Aion Mesh Node Setup ==="

# 1. Install deps
if ! command -v nmap &>/dev/null; then
    echo "Installing nmap..."
    sudo apt-get install -y nmap 2>/dev/null || sudo pacman -S nmap 2>/dev/null || brew install nmap 2>/dev/null
fi

# 2. Install node.py (verify before making it executable)
TMP_NODE="$(mktemp)"
trap 'rm -f "$TMP_NODE"' EXIT
if [ -f "$NODE_PY" ]; then
    cp "$NODE_PY" "$TMP_NODE"
    echo "Using local node scanner from $NODE_PY"
else
    # Remote install — refuse anything unpinned/unverified (MITM => root RCE)
    if [ -z "$MESH_REF" ] || [ -z "$MESH_NODE_SHA256" ]; then
        echo "ERROR: remote install needs MESH_REF (commit SHA) and MESH_NODE_SHA256." >&2
        echo "       Refusing to fetch node.py from a mutable branch." >&2
        exit 1
    fi
    echo "Downloading node.py at $MESH_REF ..."
    curl -fsSL -o "$TMP_NODE" \
        "https://raw.githubusercontent.com/ilPez00/aion/${MESH_REF}/src/aion/agents/node.py"
    echo "${MESH_NODE_SHA256}  ${TMP_NODE}" | sha256sum -c - || {
        echo "ERROR: node.py sha256 mismatch — aborting." >&2; exit 1; }
fi
sudo cp "$TMP_NODE" "$NODE_SCRIPT"
sudo chmod +x "$NODE_SCRIPT"
echo "Installed $NODE_SCRIPT"

# 3. Register cron
CRON_JOB="*/5 * * * * $NODE_SCRIPT >> $CRON_LOG 2>&1"
(crontab -l 2>/dev/null | grep -v aion-mesh-scan; echo "$CRON_JOB") | crontab -

# 4. Run once now
echo "=== Initial scan ==="
"$NODE_SCRIPT"

echo ""
echo "=== Node active ==="
echo "  Scan every 5 min via cron"
echo "  Log: $CRON_LOG"
echo "  State: ~/.cache/aion-mesh/nodes.json"
echo "  To see live nodes: $NODE_SCRIPT"
