# Nodes — one aion, every machine

aion started machine-local. Nodes are the seam that lets one instance observe
and drive agent sessions running on your other boxes, across networks.

## The two problems, kept separate

**Observe** — see what hermes/claude/opencode are doing elsewhere: which are
running, what they cost, which one is stuck on an approval prompt.

**Control** — answer that prompt from here.

They have different solutions and different failure modes. Don't conflate them.

## Transport: ssh, not a daemon

Every remote call is ssh over an overlay network (Tailscale/WireGuard), riding
a shared `ControlMaster` connection so a call costs a round trip, not a
handshake.

The obvious alternative — run an aion HTTP daemon on every machine — was
rejected on purpose. It is a second auth surface, a second update path, and a
second thing that breaks at 2am. ssh gives auth, encryption and multiplexing
for free and needs nothing deployed. Revisit only if you need sub-second push
instead of poll.

## The seam

`src/aion/nodes.py` exposes three primitives and nothing else:

```python
node.run(argv)          -> NodeResult    # like subprocess.run, never raises
node.fetch(remote_path) -> Path | None   # copy-then-read
node.exists(path)       -> bool
```

A collector written against these works on every machine. `LOCAL` is the
degenerate case, not a special case — `LOCAL.run()` is a plain
`subprocess.run`, so single-machine users pay nothing.

### Rules the seam enforces

- **Never raises.** Collectors run on a HUD timer; one exception kills a
  poller. Timeout, missing binary and dead host all come back as a
  `NodeResult`.
- **`unreachable` ≠ `returncode != 0`.** ssh exits 255 when the *transport*
  failed. A node that is down renders as offline; `tmux` exiting 1 renders as
  "no panes". Conflating them makes a dead box look like an idle one.
- **Copy, don't mount.** sqlite over sshfs/NFS corrupts. `fetch` scp's to
  `~/.cache/aion/nodes/<node>/` with a 30s TTL, writes through a `.part` file
  so a half-copied `state.db` is never opened, and serves the stale copy if a
  node drops — a box that just went to sleep should show its last known state,
  not vanish.
- **`~` is resolved here, not by a remote shell.** BatchMode ssh has no login
  shell, and these strings get `shlex.join`'d into an argv. Hence the `home`
  field in config — it is what makes a termux node
  (`/data/data/com.termux/files/home`) work.
- **`local` is built in and not overridable.** A bad config cannot break
  single-machine boot; an unknown node name falls back to local.

## Config

`config/nodes.json` (see `nodes.example.json`). Use overlay hostnames, not LAN
IPs, so the same config works from any network.

```json
{"nodes": [
  {"name": "forge", "host": "forge.tail-scale-net.ts.net",
   "transport": "ssh", "user": "gio", "home": "/home/gio"}
]}
```

## Collectors

`collect_agents(hermes_dir, node=LOCAL)` and
`collect_sessions(hermes_dir, node=LOCAL)`, plus `*_multi(registry)` variants
that poll every node in parallel — one sleeping box must not hold the HUD
behind its ConnectTimeout.

Local keeps its fast path (pgrep per binary, direct `/proc` reads). Remote
cannot afford that shape: `/proc` is thousands of tiny reads and each pgrep
would be its own round trip. So remote scanning is **one** `ps` for the whole
process table, filtered in Python, plus **one** batched `readlink` loop for
cwds. One round trip per node, not per process.

Everything collected carries its `node`, so a merged view stays unambiguous.
`merge_agent_states` deliberately drops `unreachable`: it is per-node, and one
dead box must not render the whole fleet as down.

### The probe-detection trap

Our `ps` runs *on the remote node*, so it appears in its own output, and
`pgrep -f` semantics match anywhere in the command line. Without the
`_PROBE_MARKERS` filter, every node reports phantom agents conjured by aion's
own probes.

## tmux is the control plane

This is the load-bearing constraint: **an agent TUI running in a bare ssh
session cannot be attached to after the fact.** A tmux pane can be captured and
sent keys from anywhere, forever.

So: launch every agent session inside tmux. `agents.py` already discovers panes
and matches them to processes by tty; with a node it does that remotely too.

- Read a pane: `_capture_pane_preview(pane_id, node=node)`
- Drive a pane: `send_keys(pane_id, keys, node=node)` — uses `send-keys -l` so
  payload text is never parsed as a key name (sending the literal word "Enter"
  must not press Enter).
- Watch a pane live: the `remote-term` harness (`RemoteTermHarness`), which is
  `TermHarness` with one thing swapped — the argv handed to the pty. `ssh -tt`
  allocates a real tty on the far side, so pyte parses the same escape
  sequences it would locally and the UI needs no knowledge of nodes.

### Attach sizing gotcha

A tmux session sizes to its **smallest attached client**. Attaching aion's
110x32 pane to a session you are using at 200x50 shrinks *your* view. That is
why remote attach defaults to read-only.

To type into a remote agent, prefer `send_keys` — it needs no attach at all. If
you do want an interactive attach, set `setw -g window-size latest` on the
remote tmux.

## Not yet wired

- HUD panels do not render the node column yet; `collect_*_multi` is available
  but `collect_all` still calls the single-node path.
- `send_keys` has no intent/keybinding, so operator alerts are not yet
  actionable from the right rail.
- No node health panel, though `NodeRegistry.status()` returns the rows for it.
