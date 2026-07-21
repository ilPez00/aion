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
   keys:

   ```bash
   scp ~/.aion/token other-box:~/.aion/token
   ```

3. Add the node, from `config/layout.json`:

   ```json
   "remote_nodes": [{"id": "pi5", "host": "192.168.1.100", "port": 8765}]
   ```

   or at runtime via `Ctrl-K`:

   ```
   remote add pi5 192.168.1.100:8765
   remote run pi5 build the firmware
   ```

Requests without the token get a 401 and no handler runs. Transport is plain
HTTP — the token authenticates the caller, it does not encrypt the traffic.
Treat it as a trusted-LAN feature; do not expose these ports to the internet.
