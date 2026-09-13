#!/usr/bin/env bash
# scripts/install.sh — install aion on a fresh machine and merge it into the
# fleet network. The front door; aion-provision.sh remains the node layer.
#
#     ./scripts/install.sh                 # full run: deps, tailscale, node, merge
#     ./scripts/install.sh --no-tailscale  # skip tailnet (local-only install)
#     ./scripts/install.sh --dev           # dev extras (pytest/ruff) too
#
# Idempotent by design: every step checks before it acts, so re-running after
# a half-finished first attempt resumes instead of redoing. Nothing is ever
# deleted. No secrets live in this file — the fleet token is pasted by a
# human, never echoed, never logged.
#
# Order matters: system deps -> tailscale auth -> node (provision.sh) ->
# token -> peer merge -> verify. The network merge runs only AFTER tailscale
# reports a 100.x address, because peers discovered before auth are the LAN
# IPs that stop working the moment the machine leaves the building.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV=0; WITH_TS=1
for a in "$@"; do
  case "$a" in
    --dev) DEV=1;;
    --no-tailscale) WITH_TS=0;;
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    *) echo "unknown opt $a" >&2; exit 2;;
  esac
done

say() { printf '[install] %s\n' "$*"; }
die() { printf '[install] FAILED: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ── 1. system deps ──────────────────────────────────────────────────────────
say "checking system deps"
py="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
[ -n "$py" ] || die "no python3 found — install python3 (>=3.11) first"
ver="$("$py" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
[ "${ver%.*}" = "3" ] && [ "${ver#*.}" -ge 11 ] || die "python $ver < 3.11"
for t in git ssh curl; do
  have "$t" || die "missing '$t' — install it with your package manager first"
done
if "$py" -c "import venv, ensurepip" 2>/dev/null; then
  say "python $ver (+venv), git, ssh, curl present"
elif command -v uv >/dev/null 2>&1 || [ -x "$HOME/.local/bin/uv" ]; then
  say "python $ver (no venv module — uv will create the venv), git, ssh, curl present"
else
  die "python venv support missing and no uv — install python3-venv (apt) or uv (https://docs.astral.sh/uv/) first"
fi
have sshfs || say "sshfs not found — NAS mounts will be unavailable (optional)"

# ── 2. tailscale: install, authenticate, wait for an address ────────────────
TS_IP=""
if [ "$WITH_TS" = "1" ]; then
  if ! have tailscale; then
    if have apt-get && sudo -n true 2>/dev/null; then
      say "installing tailscale via apt"
      curl -fsSL https://tailscale.com/install.sh | sudo sh \
        || die "tailscale install failed — see https://tailscale.com/download"
    else
      die "tailscale not installed and no passwordless sudo — install it from https://tailscale.com/download, then rerun"
    fi
  fi
  if ! tailscale status >/dev/null 2>&1; then
    say "bringing tailscale up — authenticate in the browser if asked"
    sudo tailscale up || die "tailscale up failed"
  fi
  say "waiting for a tailnet address (Ctrl-C here keeps the local install)"
  for _ in $(seq 1 60); do
    TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
    case "$TS_IP" in 100.*) break;; *) TS_IP=""; sleep 5;; esac
  done
  [ -n "$TS_IP" ] || die "no 100.x address after 5 min — finish 'tailscale up' in another terminal, then rerun (progress so far is kept)"
  say "tailnet address $TS_IP"
else
  say "skipping tailscale (--no-tailscale): local install only, no peer merge"
fi

# ── 3. the node (venv, deps, listener unit) ─────────────────────────────────
EXTRAS="web"; [ "$DEV" = "1" ] && EXTRAS="dev,web"
say "provisioning node (extras: $EXTRAS)"
AION_EXTRAS="$EXTRAS" AION_REPO="$REPO" bash "$REPO/scripts/aion-provision.sh" \
  || die "provisioning failed — fix the error above and rerun"

# ── 4. fleet token (pasted, never echoed, never logged) ─────────────────────
if [ -f "$HOME/.aion/token" ]; then
  say "fleet token present"
else
  printf '[install] paste the fleet token (from any fleet machine: cat ~/.aion/token)\n'
  stty -echo 2>/dev/null || true
  read -r TOKEN
  stty echo 2>/dev/null || true
  printf '\n'
  [ -n "${TOKEN:-}" ] || die "empty token — rerun when you have it"
  umask 077
  printf '%s' "$TOKEN" > "$HOME/.aion/token"
  chmod 600 "$HOME/.aion/token"
  TOKEN=""; unset TOKEN
  say "token stored (0600)"
fi

# ── 5. merge into the network ───────────────────────────────────────────────
# Remote aion peers become remote_nodes in the USER overlay
# (~/.config/aion/layout.json) — never in the shipped config, so pulls can
# never clobber a machine's view of the fleet.
if [ -n "$TS_IP" ]; then
  say "discovering tailnet peers"
  OVERLAY="$HOME/.config/aion/layout.json"
  mkdir -p "$HOME/.config/aion"
  # stdin carries the heredoc program, so the status json goes via a file —
  # piping it would collide with the heredoc on stdin and decode empty.
  TS_JSON="$(mktemp)"
  if tailscale status --json >"$TS_JSON" 2>/dev/null; then
    python3 - "$OVERLAY" "$TS_JSON" <<'EOF' || say "peer discovery found nobody new (single-node fleet is fine)"
import json, re, sys
with open(sys.argv[2]) as fh:
    st = json.load(fh)
overlay_path = sys.argv[1]
peers = []
for p in (st.get("Peer") or {}).values():
    if not p.get("Online", False):
        continue  # offline boxes join when they come back and run this
    host = (p.get("HostName") or "").lower()
    ips = p.get("TailscaleIPs") or []
    if host and ips:
        ident = re.sub(r"[^a-z0-9-]+", "-", host).strip("-")
        if ident:
            peers.append({"id": ident, "host": ips[0], "port": 8765,
                          "label": host})
try:
    with open(overlay_path) as fh:
        overlay = json.load(fh)
except (OSError, ValueError):
    overlay = {}
if not isinstance(overlay, dict):
    overlay = {}
known = {n.get("id") for n in overlay.get("remote_nodes", []) or []}
added = [p for p in peers if p["id"] not in known]
if added:
    import os
    import shutil
    if os.path.exists(overlay_path):
        shutil.copy(overlay_path, overlay_path + ".bak")
    overlay["remote_nodes"] = (overlay.get("remote_nodes", []) or []) + added
    with open(overlay_path, "w") as fh:
        json.dump(overlay, fh, indent=2)
    print(f"added {len(added)} peer(s): " + ", ".join(p["id"] for p in added))
else:
    print("no new peers")
EOF
  fi
  rm -f "$TS_JSON"
fi

# Peer SSH key: generation is safe to automate, INSTALLATION stays deliberate
# (getting it wrong grants access — same rule as aion-provision.sh).
KEY="$HOME/.ssh/aion_$(hostname -s 2>/dev/null || hostname)"
if [ ! -f "$KEY" ]; then
  say "generating peer ssh key"
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "aion@$(hostname)" -q
fi
say "peer key ready: $KEY"

# ── 6. verify ───────────────────────────────────────────────────────────────
PORT="${AION_NODE_PORT:-8765}"
errs=0
if have ss && ss -lntH 2>/dev/null | grep -qE "[:.]${PORT}\b"; then
  say "listener holds port $PORT"
else
  say "WARNING: nothing on port $PORT — check: journalctl --user -u aion-node"; errs=$((errs+1))
fi
if [ "$(stat -c %a "$HOME/.aion/token" 2>/dev/null)" = "600" ]; then
  say "token permissions 0600"
else
  say "WARNING: token permissions wrong — expected 0600"; errs=$((errs+1))
fi
if [ -n "$TS_IP" ]; then
  got="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  [ "$got" = "$TS_IP" ] && say "tailnet still $TS_IP" || { say "WARNING: tailnet address moved"; errs=$((errs+1)); }
fi
[ "$errs" = "0" ] || die "verify found $errs problem(s) — see WARNINGs above"

say "done. This machine is on the tailnet${TS_IP:+ ($TS_IP)} with aion installed."
say "On the CONTROLLER, authorize this peer key, then in the cockpit:"
say "  remote add $(hostname -s 2>/dev/null || hostname) ${TS_IP:-<this-host>}:${PORT}"
