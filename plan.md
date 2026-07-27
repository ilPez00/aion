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

- `docs/aion-cyclops-reconcile.md` — spec-vs-repo map (what the PRS got wrong).
- `docs/physis-integration.md` — how the brain plugs into the loops.
- `docs/install-mobile.md` — PWA/APK install path + the HTTPS caveat.
