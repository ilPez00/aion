# aion — plan & status

**aion = splitscreen HUD + application desktop.** 8 unified workspaces, driven by
one `Intent` bus across keyboard / joystick / voice / CyclUno deck / Colmi ring.

## Workspaces

| # | Workspace | Absorbs | Content |
|---|-----------|---------|---------|
| 1 | Desktop | Projects | System status, launcher, todos, sessions, data, GPU, agents, projects, activity, quick commands |
| 2 | Models | — | Harness list (vram/tier/running) |
| 3 | Tasks | Board | Task registry (progress/history) + kanban boards |
| 4 | Agent | Agents + Swarm | Agent entity cards + swarm dashboard + LLM chat |
| 5 | Vault | Memory | Notes graph + memory facts |
| 6 | System | Physis + Health | CPU/RAM/disk/net/GPU + processes + health + coherence brain + **CYCLOPS panel** |
| 7 | Term | — | Live embedded terminal |
| 8 | Settings | Skills + Hermes | Provider env vars + installed skills |

## Ecosystem

- **aion** — the cockpit (this repo): Textual TUI + web HUD, harness engine, physis brain.
- **Cyclops** — the wearable/physical trio feeding aion:
  - **Cycluno** — Arduino deck (serial): joysticks + buttons + MODE. `deck/{link,protocol,gamepad}.py`. *(No rotary encoder — removed; the PRS is stale on this.)*
  - **Colmi R02 ring** (BLE): HR/SpO2/battery telemetry + accel-derived taps. `deck/ring.py` + `input.RingInput`.
  - **XIAO pendant** (cam/mic): scaffold only. `deck/pendant.py`.

## Status — done

- Core cockpit: harnesses, Intent bus, Fleet, remotes, 494+ tests.
- **Factory loop** + **DeepResearch loop** (pure engine + thin harness).
- **Factory stall detection** — bails a spinning Ralph loop (`STOP_STALLED`), pure/local.
- **physis_pro brain wired into both loops** — per-iteration coherence (opt-in),
  outcome recorded (+1 flow / −1 block), research runs ingested into the holarchy.
  See `docs/physis-integration.md`.
- **Colmi R02 ring** — telemetry + `TapDetector` "button" + `CYCLOPS` HUD panel.
  Verify offsets against hardware: `python scripts/verify_ring_accel.py`.
- **HITL approval gates** (`hitl.py`) — fail-closed; a pending gate captures the
  ACTIVATE any device emits (deck/joystick/ring/Enter) as approve, BACK as reject;
  destructive prompts + `requires_approval` harnesses gate `_spawn`. Voice + HUD wired.
- **Web HUD is a PWA** — installable; LAN-reachable via `AION_WEB_HOST=0.0.0.0`.
  See `docs/install-mobile.md`.
- **Graph file manager** (`fsgraph.py` + `/api/fs/*`) — physis-compatible
  payload, local TF-IDF + spherical k-means clustering, sandboxed to
  `AION_FS_ROOT`. Preview + move from the HUD.
- **Web HUD rebuilt around one organic graph renderer** — Files, Vault and
  System are the same canvas force layout with different adapters; each has a
  keyboard/screen-reader list twin. Responsive to 375px (bottom nav + bottom
  sheet inspector), WCAG-AA palette shared with the TUI, reduced-motion aware.
- **Launcher usability** — `./aion.sh graph|doctor|help`; `AION_WEB_PORT`
  honoured (PTY websocket follows at +1) so two HUDs can coexist.
- **Agent process graph** (`procgraph.py` + `/api/agents`) — fleet instances,
  harnesses, tasks and swarm deps as one organic graph, reconstructed from the
  on-disk checkpoints so it works with no cockpit running. Orphan harnesses
  surfaced rather than dropped.
- **Ctrl-K palette** (`/api/search/all`) — one search across modules,
  harnesses, tasks, task logs, instances, notes, filenames and file contents;
  every hit jumps to its node.
- **Navigation** — hash routing + browser back/forward, clickable
  breadcrumbs, Backspace to climb, click-a-hub to isolate a cluster,
  directional arrow traversal, Tab to walk graph neighbours.
- **Live push** (`procgraph.fingerprint/diff` + `/ws/events`) — the daemon
  watches the cockpit's checkpoint files and pushes deltas; the graph patches
  in place (no rebuild, pan/zoom/selection preserved) and pulses state
  transitions. Idle cost is a hash of a few small files at 4Hz, nothing sent.
  Falls back to polling with a visible liveness dot.

- **Swarm persistence** (`SwarmStore`) — the dependency DAG survives a restart
  and is visible cross-process; in-flight agents restore as IDLE.
- **Worktrees** (`worktrees.py` + `/api/worktrees`) — repos/worktrees/branches
  as a graph, tasks linked to the tree they mention, parallel scan. Read-only.
- **Obsidian** — the web Vault now resolves the real vault
  (`AION_VAULT` > recorded setup answer > repo `notes/`); fixed a path
  traversal in `/api/notes/content` while wiring it.
- **Open in editor** (`opener.py`) — allowlisted editors (zed/code/…),
  argv-only, sandboxed paths.
- **Notifications** (`notify.py`) — opt-in webhook for failed/stalled/gate
  events only, deduped, never raises, URL redacted from logs.

- **Cross-instance routing** (`routing.py` + `/api/route`) — drag a task onto
  an instance to run it there. Fail-closed (no dispatch without `confirm`),
  target resolved from discovery only, every decision explained with a score
  breakdown and per-candidate rejection reasons.

- **TUI surfaces in the web HUD** (`bridge.py`) — Desk (todos/memory/apps/
  modes/agents/profile), Board (kanban), Settings (providers/skills/paths) and
  a real PTY Term. Reads the same `~/.aion/shared/` stores the cockpit writes;
  todos and memory are editable from the browser.
- **One-command run** — `./aion.sh up` (physis + HUD + browser), `down`,
  `status`.

- **HITL gates in the web HUD** (`hitl.GateStore` + `/gate` transport) —
  pending gates published for cross-process display and answered over the
  authenticated transport. The published file is display-only; a forged
  approval in it releases nothing.

- **Voice control of the web HUD** (`voicecmd.py` + `/api/voice`) — spoken
  phrases drive navigation, scanning, filtering, isolation and view controls.
  Voice can DENY an approval gate but never grant one; low confidence is shown,
  not executed.
- **Conversational voice** — continuous listening with spoken replies, and an
  LLM fallback (`voicecmd.understand`) that maps ANY phrasing onto a validated
  action. The model's reply is untrusted: schema-checked, args whitelisted,
  gate decisions forced to reject. `./aion.sh up` opens Chrome, since the Web
  Speech API is Chrome-only.

- **SSH peers** (`sshlink.py` + `./aion.sh peers` + `/api/peers`) — aion
  instances on OTHER machines, reached through an `ssh -L` tunnel so the
  existing HTTP transport, routing and gates work unchanged and neither end
  binds a public interface. Per-peer keys, `restrict,permitopen=` in
  authorized_keys (no shell, no pivot), argv-injection validation, orphaned
  tunnels reaped. Peers appear in the Agents graph and as routing targets;
  dispatch is still fail-closed. See `docs/ssh-peers.md`.

- **Swarm across machines** — a step can name an instance (`instance_for` in
  the add form) and runs there via the existing token-authenticated transport,
  while the rest of the DAG runs locally. Remote work cannot announce itself on
  this process's bus, so it is polled per task (`GET /task?id=`) rather than
  read from `/status`, whose 20-task cap drops exactly the completions the
  watcher exists to see. A silent poll is a miss, not a failure — four
  consecutive misses before a peer counts as lost, so a sleeping laptop does
  not cancel live work. Spawn and poll are non-blocking (`PENDING` +
  `attach`/`deliver`): the cockpit calls these from inside its own event loop.

- **A budget stops a swarm that is working perfectly** (`swarmbudget.py`) —
  admission capped parallelism and VRAM, neither of which is money, so a DAG
  left running unattended could sit inside both limits and spend all night.
  Reserve-then-reconcile: the prompt is known before a step runs and the output
  is not, so admission holds an estimate and the real figure replaces it at the
  end. Without the hold, every step admitted in one tick sees the same
  "committed so far". Failed-but-ran settles; never-started and cancelled
  release. It is a governor and not accounting — external CLIs report stdout
  and an exit code, not tokens, so the figures are characters over four times a
  configured price and every payload carries `estimated: True`.

- **`swarm plan <goal>` builds the DAG for you, in the terminal** — the
  planner (`swarmplan.py`) validated a model-proposed DAG, capped its steps,
  refused cycles and unresolvable dependencies… and only the browser could
  reach it. In the cockpit, building a DAG meant one `swarm add <name> <goal>
  << deps` line per step, in an order `add_checked` accepts — so the surface
  people actually use had the worst way to do the thing this program is for.
  `swarm plan` now proposes, `swarm apply` creates, `swarm run` starts: three
  decisions, because every step is a prompt a harness will execute. The
  proposal is HELD, drawn in the same wave view the live swarm uses (bars
  dropped — nothing has run, so they would be a column of identical noise),
  and `apply` re-validates exactly the steps that were shown rather than
  re-planning, since a non-deterministic re-roll would make the review
  decorative. The call runs on a worker thread: a 30s model call on the event
  loop is a frozen cockpit — no keystrokes, no task updates, no heartbeat.

- **The DAG is drawn in running order, and says why it stopped**
  (`swarmview.py`) — the old view was a flat list with `← deps` glued on the
  end, which answers "what steps exist" (a question nobody asks) and cannot
  answer either question a running DAG actually gets asked: what runs next, and
  who is holding up whom. `waves()` layers the steps topologically, so reading
  order IS running order; a cycle is drawn as its own labelled wave rather than
  silently dropped, and a dependency naming no step is called out on the row
  instead of looking like patience. `explain()` turns the state into the one
  sentence the operator needs — "2 ready — `swarm run` to start", "blocked: b
  needs a, which failed", "a retries in 30s (attempt 2)" — because "nothing is
  running" has six causes and six different next moves. The same sentence is
  computed server-side for the web HUD, so the two views cannot phrase the same
  swarm differently, and each HUD node carries its `wave` so a force graph has
  a reading order at all. Also fixed here: `swarm status` stored the legacy
  TEXT dashboard while every consumer read the dict the bus publishes, so the
  one command whose whole job is "show me the swarm" left the panel raising
  `AttributeError` on `.get`.

- **A failed step is no longer the end of the DAG** (`swarmpolicy.py`) — one
  answer to a failure, and it was "stop": the agent went FAILED, `dep_state`
  blocked every dependent, and an unattended swarm sat dead until morning,
  including when the cause was a tunnel that dropped for four seconds. Now the
  failure is classified (transient / permanent / unknown, off the message,
  because CLIs report an exit code and not a taxonomy), retried with
  exponential backoff up to `max_attempts`, and dead-lettered with the reason
  retrying stopped and the list of steps stuck behind it. A retrying step goes
  back to IDLE rather than FAILED — FAILED blocks dependents, and a step we
  intend to run again in ten seconds has blocked nothing yet. Three details
  that would each have made it a liability: `attempts` is checkpointed (a
  restart that reset it turns a bounded policy into an unbounded one), the
  ledger banks the previous attempt so retries are charged rather than free,
  and the cockpit heartbeat calls `due_for_retry()` — a backoff is the one
  thing that becomes startable with no event to announce it. Default is
  `max_attempts=1`, i.e. exactly the old behaviour: an upgrade must not start
  re-running, and re-paying for, work nobody asked to be re-run. Configure with
  `"swarm_retry": 3` or the full dict.

- **A restart adopts remote work instead of re-running it** — `restore()` was
  called nowhere, so the DAG survived on disk and in the web HUD while the
  cockpit came back believing there was no swarm. And once restored, every
  WORKING agent was reset to IDLE — right for a local step whose coroutine
  died, exactly wrong for one pinned to a peer that never noticed we left,
  where it means a second copy of the same job. The task id an agent owns is
  now checkpointed, and `rehydrate()` re-attaches the watch.

- **One state machine for agents and tasks** — a swarm agent is not a process;
  it owns a task id, and the task is what a harness can suspend. So `pause` and
  `resume` have no agent-level implementation at all: `agentctl.route` decides
  them against the real task state and the store applies them through
  `control_task`, the same call a keypress in the cockpit makes. `swarm.can`
  stopped carrying its own copy of the terminal-state and rerun rules, the two
  vocabularies meet in `AGENT_AS_TASK` (a status added without a task meaning
  fails the suite), and a step pinned to another instance is paused over the
  same authenticated transport that polls it. The typed `swarm run` / `add` /
  `stop` now go through `swarm_command` too — `run` used to flip statuses to
  WORKING and spawn nothing, so a DAG typed into the cockpit sat at layer one
  forever while the identical DAG driven from the HUD ran.

- **Swarm planning** (`swarmplan.py`) — describe a goal, get a reviewed DAG.
  `decompose()` created an empty plan nobody read, so every swarm had to be
  hand-built. A model proposes; almost everything it says is refused: step
  count capped, cycles rejected at creation (not discovered later by
  `stalled()`), invented dependencies refused, hallucinated harnesses dropped
  to the default, names charset-checked. Propose → review the whole DAG →
  create → run are four separate decisions, and the reviewed steps are what
  get applied — re-planning on commit would create a DAG nobody read.

- **Swarm executor** (`swarmrun.py`) — the DAG now RUNS. Nothing previously set
  a swarm agent to DONE, so `run_ready()` moved layer one to WORKING and layer
  two waited forever (`run_all()` existed only in a docstring). An agent's goal
  is now spawned as a real task through the cockpit's gated path — inheriting
  HITL, physis classification, checkpointing and cancellation — and its
  terminal state advances the agent, which pumps the scheduler again. Pure
  `admit()` enforces max-parallel and a VRAM budget (naming any agent too big
  to ever fit, rather than starving it silently); pure `prompt_for()` feeds
  upstream output into downstream prompts, so a dependency means "take input
  from", not just "wait for". `stalled()` explains a stopped DAG, cycles
  included.

- **Swarm control from the web HUD** (`SwarmOrchestrator.control/run_ready/
  stop_all/add_checked` + `/swarm` on the transport + `/api/swarm`) — start,
  cancel, retry and remove one agent; add an agent with dependencies; run every
  ready agent or stop everything. Refusals name the blocking dependency rather
  than saying "blocked". Removal is refused while anything still depends on the
  agent (deps are by NAME, so deleting the node silently makes them
  unsatisfiable). Fixed `agents_ready()` counting a FAILED or CANCELLED
  dependency as satisfied — it and `blocked_agents()` disagreed about the same
  agent, and `swarm run` would start a step whose input never arrived.

- **Settings, expanded** (`settings.py` + `/api/settings{,/harness}`) — 8
  sections, 29 fields, declared once as a schema and rendered generically, so a
  new option is one Python entry and zero JS. Fleet thresholds, persona, web
  voice, graph detail, deck, ring, notifications, paths; plus a per-harness
  table (enable, tier, VRAM, and `requires_approval` — inserting a human into
  the loop is now one click). Validation is server-side and total: unknown keys
  dropped, out-of-range rejected with a reason rather than clamped, read-only
  paths refused, secrets redacted on read and never echoed back. Fixed
  `write_json_atomic` escaping non-ASCII, which turned every workspace icon
  into `\uXXXX` on any save.

- **Agent control from the web HUD** (`agentctl.py` + `/api/agents/{control,spawn}`
  + `/task` on the transport) — spawn a task on a harness, pause/resume/cancel/
  rerun a running one, from the browser. The HUD never runs a harness: it asks a
  live cockpit over the authenticated transport and that cockpit applies its
  HITL gates. Legality lives in ONE place (`agentctl.legal`), shared by the TUI
  keybindings and the web, so they cannot drift. Fixed three bugs on that path:
  `on_run` dropped the harness argument, `on_cancel` passed an id where a Task
  was required (every remote cancel 500'd), and `_respawn` bypassed the gate so
  an approved destructive prompt could be re-run with no approval.

- **Level of detail in the graph** — the renderer draws a fixed number of
  nodes per unit of screen area and defers the rest, so zooming in reveals new
  elements as room appears rather than crossing a tuned threshold. Hubs, the
  selection and search hits are exempt; the list view and search stay complete;
  the deferred count is on screen and in the screen-reader description.

## Roadmap — next

1. **Colmi accel offsets** — run the verify harness against the real ring, pin
   `decode_accel()`, tune `TapDetector.threshold`. *(blocks the ring "button" in field)*
2. **Real APK** — pair phone Wireless Debugging, `tailscale serve` HTTPS the PWA,
   Bubblewrap build (`twa-manifest.json`), `adb install`. *(blocked on phone pairing code)*
3. **Pendant → physis ingest** — the transport is **done**: HTTP pull against the
   endpoints the cyclops firmware already serves (`/snap`, `/audio.wav`,
   `/stream`), in `deck/pendant.py`, LAN-only, stdlib. BLE was never viable for
   VGA JPEGs at the ~2 KB/s notify throughput bring-up measured. What is left is
   feeding `PendantEvent` blobs into the physis context (vision → sightings,
   audio → transcriber) and registering the link in `ui/app.py` next to
   `DeckInput`/`RingInput`. Point it with `AION_PENDANT_HOST`.
4. **HUD coherence stream** — feed `Iteration.coherence` into the DAG edge animation.
   *(the live channel now exists; this is the next payload to put on it)*
5. **Research priming** — use physis `reconstruct()` to skip queries a past run covered.
6. **assetlinks.json route** in `aion_web.py` for TWA trust (needed by the APK).

## Verification

```bash
cd /home/gio/aion
.venv/bin/python -m pytest tests/ --ignore=tests/test_term.py -q
TERM=xterm-256color timeout 6 .venv/bin/python -m aion.ui.app        # TUI smoke
AION_WEB_HOST=0.0.0.0 .venv/bin/python scripts/aion_web.py           # web HUD on LAN
```

## Reference docs

- `docs/ssh-peers.md` — reaching aion on another machine, and the key restrictions.
- `docs/aion-cyclops-reconcile.md` — spec-vs-repo map (what the PRS got wrong).
- `docs/physis-integration.md` — how the brain plugs into the loops.
- `docs/install-mobile.md` — PWA/APK install path + the HTTPS caveat.
