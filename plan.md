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
