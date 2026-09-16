# Fleet — many instances, one cockpit

aion can run several times at once: a full-screen cockpit, a half-screen HUD,
a headless box in the corner. The Fleet workspace (`🌐`, key `9`) shows all of
them — the one you are looking at, its siblings on this machine, and remote
nodes over the network.

## Running a second instance

```bash
./aion.sh                    # instance "main", port 8765
AION_INSTANCE=hud ./aion.sh  # instance "hud",  port 8801
```

The name picks the port (`8765 + crc32(name) % 100`), so a peer's port follows
from its name — no registry, no config entry. `main` keeps 8765 for
compatibility with existing setups.

## Where state lives

```
~/.aion/
  token                     shared secret (0600)
  shared/                   your data — every instance reads and writes it
    todos.md  memory.json  boards.json  agents.json  vault/
  instances/
    main/  session.json  meta.json
    hud/   session.json  meta.json
```

Tasks are per-instance: a task belongs to the process that spawned it, and
that process cannot resurrect another's coroutines. Everything else is your
data, so there is one copy and every cockpit sees the same thing.

Files from before this layout are moved on first launch. Migration never
overwrites: if a destination already exists the old file is left in `~/.aion/`
for you to sort out by hand.

## Getting there

The Fleet workspace is workspace 9 — `9` on the keyboard, positional on a
joystick or the deck (navigate right along the rail), or by voice: "go to
fleet", "show network", "nodes". Voice names come from each workspace's id,
its title, and a small alias list in `input.py`, built from config so a
workspace added later is spoken-reachable without touching the voice code.

## Runs — watching agent work

The Runs workspace (`⟳`, or voice "go to runs" / "processes" / "results")
collects every task from an agent-tagged harness (web, research, factory,
opencode, cyclops) into two tabs:

- **Processes** — what is running now. Progress bars, oldest-first so a
  long-running loop is at the top. `x` kills a runaway; `p` pauses.
- **Results** — what finished, newest-first, with the output it produced and
  a stop reason. `Enter`/`r` re-runs a failed or exhausted one.

Switch tabs with `t`, or `Enter` on the tab bar. A harness joins Runs by
carrying the `agent` context tag in config — nothing here needs editing.

### Real factory agents

The default `factory` harness drives `claude -p`, telling the agent to end
with `TASK_COMPLETE` when finished. Point it at any installed agent by editing
`extra.command` in `config/layout.json` (`{p}` prompt, `{last}` prior output,
both shell-quoted; `{n}` iteration):

```jsonc
// objective completion — loop until the tests pass, not until the agent says so
"extra": {
  "command": "codex exec 'Fix failing tests. {p}. Last: {last}'",
  "done_command": "python -m pytest -q",   // exit 0 == done
  "per_iter_timeout": 300
}
```

`done_command` (a shell check that exits 0 when complete) is more trustworthy
than `done_marker` (a string the agent prints) because it doesn't rely on the
agent's self-report. `max_steps` caps the loop either way.

## Settings

The Settings workspace (`⚙`) opens with a FLEET block listing every value, what
it currently is, and where it came from. Change one from the palette:

```
fleet show                        # values + source
fleet set remote_offline_s 90
fleet set listen lan
fleet token show | fleet token rotate
```

Values persist to the `"fleet"` block of `config/layout.json`. Precedence is
**environment > config > default**, so `AION_INSTANCE` / `AION_LISTEN` on the
command line always win for one launch; when they do, Settings shows the value
in amber and names the variable rather than pretending the config applies.

| key | default | effect |
|---|---|---|
| `instance` | `main` | name → state root and port. Restart. |
| `listen` | `local` | `lan` binds 0.0.0.0. Restart. |
| `heartbeat_s` | 5 | how often this instance advertises |
| `local_stale_s` / `local_offline_s` | 15 / 30 | same-machine thresholds |
| `remote_stale_s` / `remote_offline_s` | 20 / 60 | over-the-network thresholds |

A stale threshold is clamped below its offline threshold — otherwise `stale`
becomes unreachable and nodes jump straight from live to offline.

`fleet token rotate` locks out every other machine until you copy the new
secret across, so the command says so rather than leaving you to discover it.

## Discovery

Each instance writes `instances/<id>/meta.json` every 5s and deletes it on
exit. Peers read the directory. A SIGKILLed instance can't clean up, so
whoever notices reaps its file by checking the pid.

Health has four states, not two:

| state | meaning | local | remote |
|---|---|---|---|
| `live` | answering promptly | < 15s | < 20s |
| `stale` | answering, lagging | < 30s | < 60s |
| `offline` | was reachable, now silent | ≥ 30s | ≥ 60s |
| `unknown` | configured, never contacted | — | — |

Remote thresholds are more patient because the network is the unreliable part,
not the node. Load buys no grace: a node too busy to answer is exactly the one
you want flagged.

## Reaching another machine

`POST /run` executes commands on the receiving box, so the listener is bound to
loopback and requires a shared token.

1. Opt into network exposure on the machine being controlled:

   ```bash
   AION_LISTEN=lan ./aion.sh
   ```

   The Fleet footer reads `LAN` in amber when exposed, `this machine only`
   otherwise.

2. Copy the secret to every machine in the fleet — one secret, not per-node
   keys. On a fresh box the installer prompts for it (pasted, never logged):

   ```bash
   ./scripts/install.sh   # deps, tailscale auth, node, peer merge
   ```

   or by hand:

   ```bash
   scp ~/.aion/token other-box:~/.aion/token
   ```

3. Add the node. The installer discovers online tailnet peers and writes
   them to the *user* overlay (`~/.config/aion/layout.json`), never the
   shipped config — pulls can never clobber a machine's view of the fleet:

   ```json
   "remote_nodes": [{"id": "omo", "host": "100.116.39.57", "port": 8765}]
   ```

   Reach peers by Tailscale IP (stable per device), not LAN IPs (they change
   the moment the machine leaves the building). Or at runtime via `Ctrl-K`:

   ```
   remote add omo 100.116.39.57:8765
   remote run omo build the firmware
   ```

Requests without the token get a 401 and no handler runs. Transport is plain
HTTP — the token authenticates the caller, it does not encrypt the traffic.
Treat it as a trusted-LAN feature; do not expose these ports to the internet.

## Shared agentic history (agent-bridge)

Beyond live instances, the fleet keeps one shared history every agent on
every machine reads and writes: inbox messages plus a learnings store.
The on-disk protocol matches
[EthanSK/agent-bridge](https://github.com/EthanSK/agent-bridge) 1:1, so the
same `~/.agent-bridge/` files work with the real CLI where it is installed —
aion needs no Node, `src/aion/agentbridge.py` speaks the shapes natively.

```
~/.agent-bridge/
  inbox/aion/<uuid>.json          pending messages for this cockpit
  inbox/.archive/aion/            consumed messages (acked, not deleted)
  inbox/.processed                ledger of consumed ids (stops re-delivery)
  shared-context/learnings.ndjson full replica, append-only, uuid-keyed
```

Palette (`Ctrl-K`): `bridge inbox` · `bridge send <target> <text> [--host H]` ·
`bridge ack <target> <id-prefix>` · `bridge learn <title> | <body> [#tags]` ·
`bridge search <query> [#tag]` · `bridge sync [hosts...]`. The Fleet panel
shows pending count + learnings total; `/api/fleet/sessions` and
`/api/fleet/models` expose the same to the web HUD.

Sync is append-only union by id in both directions — pushes, pulls and
replays are idempotent, and concurrent appends on either side cannot clobber
each other. Peers come from `fleet.json` (override: `AGENT_BRIDGE_HOSTS`).
Rule of the road, same as upstream: search before debugging something another
agent may have solved; record anything a different agent on a different
machine would benefit from. Per-harness memory stays where it is — this store
is additive, never a replacement.

## SentinelX agents (allowlisted shell per host)

SentinelX ([sentinelx.app](https://sentinelx.app), agent
`pensados/sentinelx-cloud-core`) gives an MCP client — Claude.ai, ChatGPT, any
MCP client — an **allowlisted, auditable shell** on a host, over a single
*outbound* WebSocket to the hub `mcp.sentinelx.app`. No inbound port. The
rollout, host_ids and the install path live in the randomesh repo
(`docs/SENTINELX.md`, `scripts/fleet/install-sentinelx*.sh`); this is the
cockpit's half of it.

`src/aion/sentinelx.py` probes each node in **one SSH round trip** and folds the
result into the Fleet workspace as a fifth section:

```
⛨ SentinelX  3/5 live  · sentinelx-cloud-core
  ● pansa     host_e4f4b084b 86 cmds sudo sess_aed361
  ● omo       host_07ef13a4f 86 cmds sudo conn ?
  ○ air       DOWN ssh: connect to host 100.66.51.47 port 22
  · pi        not installed
  ◌ feather   unenrolled host_1c3042784
     ↳ enroll: https://mcp.sentinelx.app/auth/dashboard/enroll?host_id=…
sentinelx start|stop|restart <host>
```

Five states, because they need five different reactions: `live` · `stopped`
(installed + enrolled, unit down) · `unenrolled` (needs a browser visit) ·
`absent` (never installed) · `down` (host unreachable). Hosts come from
randomesh `CONFIG.md → fleet.json` (`AION_FLEET_CONFIG` to point elsewhere), so
adding a node there adds a row here.

Palette / command bar: `sentinelx list` · `sentinelx status <host>` ·
`sentinelx start|stop|restart <host>` · `sentinelx enroll <host>` ·
`sentinelx connector`.

Two rules worth keeping:

* **The enrollment token is never read.** `/etc/sentinelx/identity.json` is
  `0600 root:sentinelx`; the HUD reports *that it exists*, never its contents.
  For a host that lacks it the panel prints the enrollment URL and leaves the
  token to the operator.
* **No password plumbing.** Lifecycle runs `sudo -n systemctl …`, so it works
  only where the operator granted a *scoped* rule for that one unit:

  ```
  # /etc/sudoers.d/gio-sentinelx  (0440 root:root, validate with visudo -c)
  gio ALL=(root) NOPASSWD: /usr/bin/systemctl start sentinelx-cloud-core, \
                           /usr/bin/systemctl stop sentinelx-cloud-core, \
                           /usr/bin/systemctl restart sentinelx-cloud-core, \
                           /usr/bin/systemctl is-active sentinelx-cloud-core
  ```

  (`sentinelx.grant_hint(alias=...)` prints the whole `ssh <host> … visudo -c`
  line.) Without the grant the action fails and says so, instead of aion ever
  handling a password. The panel shows `conn ?` for a live agent when this user
  cannot read the journal — the live `connected; session=…` line needs
  membership of the `systemd-journal` group; without it, `unit active` is still
  accurate and is the actionable signal.

The `sudo` tag on a row means the **agent** (user `sentinelx`) holds
`NOPASSWD: ALL` — root-equivalent on that host. Revoke per host with
`rm /etc/sudoers.d/sentinelx && systemctl restart sentinelx-cloud-core`.
