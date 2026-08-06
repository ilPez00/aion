#!/usr/bin/env bash
# aion-provision.sh — make this machine a fleet node.
#
# Run ON the machine being provisioned:
#
#     ./scripts/aion-provision.sh                 # default port 8765
#     AION_NODE_PORT=8775 ./scripts/aion-provision.sh
#
# Idempotent by design. Bringing up pansa and air by hand produced a different
# result on each — one venv without pip, one editable install pointing at a
# stale checkout, two different ports, one machine with no token — and every
# difference had to be rediscovered later. A script is the difference between
# "a node" and "whatever that box ended up as".
#
# What it does NOT do, on purpose:
#   - install the SSH peer key. That is the controller's half, and it is the
#     one step where getting it wrong grants access rather than withholding
#     it, so it stays a deliberate act (`aion.sh peers add` prints the line).
#   - copy the fleet token. It is a shared secret; it is moved by a human who
#     knows which fleet this machine is joining.
# Both are printed as next steps instead.
set -euo pipefail

REPO="${AION_REPO:-$HOME/aion}"
PORT="${AION_NODE_PORT:-8765}"
say() { printf '[provision] %s\n' "$*"; }
die() { printf '[provision] FAILED: %s\n' "$*" >&2; exit 1; }

# `git rev-parse`, not `[ -d .git ]`: in a WORKTREE `.git` is a file pointing
# at the real git dir, and air's node runs from one. The directory test
# rejected a perfectly good checkout.
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || die "no aion checkout at $REPO (set AION_REPO)"
cd "$REPO"

# ── 1. python + deps ────────────────────────────────────────────────────────
# uv when it is here (it is what created these venvs), pip otherwise. Checked
# rather than assumed: pansa's venv has no pip at all, which is only
# discovered at the moment you need it.
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$REPO/.venv/bin/python" ]; then
  say "creating venv"
  if [ -x "$UV" ]; then "$UV" venv "$REPO/.venv"; else python3 -m venv "$REPO/.venv"; fi
fi
PY="$REPO/.venv/bin/python"

say "installing aion + test deps"
if [ -x "$UV" ]; then
  "$UV" pip install --python "$PY" -q -e ".[dev,web]" || die "uv install failed"
else
  "$PY" -m pip install -q -e ".[dev,web]" || die "pip install failed"
fi

# An editable install pointing at a DIFFERENT checkout is the trap air fell
# into: tests in one tree silently importing aion from another. Verify the
# package resolves to this repo rather than trusting that it does.
RESOLVED="$("$PY" -c 'import aion, pathlib; print(pathlib.Path(aion.__file__).resolve().parents[1])' 2>/dev/null || true)"
EXPECT="$("$PY" -c "import pathlib; print(pathlib.Path('$REPO/src').resolve())")"
[ "$RESOLVED" = "$EXPECT" ] || die "aion resolves to $RESOLVED, not $EXPECT — editable install points at another checkout"
say "aion resolves to $RESOLVED"

# ── 2. state dir ────────────────────────────────────────────────────────────
mkdir -p "$HOME/.aion"
if [ -f "$HOME/.aion/token" ]; then
  say "token present ($(sha256sum "$HOME/.aion/token" | cut -c1-8)…)"
else
  say "NO TOKEN — this node cannot join a fleet until one is copied here"
fi

# ── 3. the listener ─────────────────────────────────────────────────────────
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Stop our own node BEFORE testing the port. Re-provisioning a working machine
# must not fail because that machine is working — the first version of this
# refused on pansa for exactly that reason, which is the opposite of
# idempotent.
if systemctl --user is-active --quiet aion-node 2>/dev/null; then
  say "stopping the running node to re-provision"
  systemctl --user stop aion-node
fi

# Now anything still holding the port belongs to somebody else. Refuse rather
# than fight for it: air already had 8765 and 8766 taken by an unrelated
# service, and a node that loses the bind race dies in a restart loop whose
# real cause is buried in a log.
if command -v ss >/dev/null 2>&1 && ss -lntH 2>/dev/null | grep -qE "[:.]${PORT}\b"; then
  die "port $PORT is held by another process — rerun with AION_NODE_PORT=<free port>"
fi

mkdir -p "$HOME/.config/systemd/user"
install -m644 "$REPO/scripts/aion-node.service" "$HOME/.config/systemd/user/aion-node.service"
if [ "$PORT" != "8765" ] || [ "$REPO" != "$HOME/aion" ]; then
  mkdir -p "$HOME/.config/systemd/user/aion-node.service.d"
  cat > "$HOME/.config/systemd/user/aion-node.service.d/override.conf" <<EOF
[Service]
Environment=AION_NODE_PORT=${PORT}
WorkingDirectory=${REPO}
ExecStart=
ExecStart=${PY} ${REPO}/scripts/aion_node.py --port \${AION_NODE_PORT}
EOF
  say "override: port ${PORT}, repo ${REPO}"
fi

# XDG_RUNTIME_DIR is exported above, before the first systemctl call: an ssh
# command that is not a login shell has no session bus, and without it
# `enable` silently reports the unit as not-found.
systemctl --user daemon-reload
systemctl --user enable --now aion-node
sleep 3
systemctl --user is-active --quiet aion-node || die "unit did not start: journalctl --user -u aion-node"
say "node active on 127.0.0.1:${PORT}"

# Without lingering the user manager exits with the last session and the peer
# goes dark until somebody logs in -- which on a headless box may be never.
if command -v loginctl >/dev/null 2>&1; then
  if [ "$(loginctl show-user "$(id -u)" -p Linger --value 2>/dev/null)" = "yes" ]; then
    say "lingering enabled"
  else
    say "NOT lingering — run: sudo loginctl enable-linger $USER"
  fi
fi

say "done. On the CONTROLLER:"
say "  ./aion.sh peers add $(hostname) ${USER}@$(hostname -I 2>/dev/null | awk '{print $1}') --key ~/.ssh/aion_$(hostname) --remote-port ${PORT}"
say "  then install the printed line in ~/.ssh/authorized_keys here"
