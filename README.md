# aion

**Splitscreen HUD + application desktop** — not a chat assistant.

aion is a customizable multi-harness **desktop shell** and **stats HUD** for
half a screen (or a full console). Drive it with keyboard, trackpad,
joystick/gamepad, optional voice, the **CyclUno deck** (physical console:
sticks + buttons + SPI TFT) that navigates workspaces one-handed and doubles as
a Linux gamepad for programs aion spawns, and the **Colmi R02 ring** (BLE:
HR/SpO2/battery telemetry + accel-derived taps). Together the deck + ring +
XIAO pendant form the **Cyclops** physical layer. See `docs/DECK.md`,
`docs/IDENTITY.md`, and `docs/aion-cyclops-reconcile.md`.

Run it more than once — a cockpit and a HUD side by side, or a box in the
corner you drive over the network — and the **Fleet** workspace shows every
instance at once. See `docs/FLEET.md`.

The core idea: every input device emits the *same* `Intent` objects; every
backend is a swappable `Harness` (apps, shells, agents, monitors). The UI
**renders status and manages processes**; it does not exist to chat. Harnesses
push live task progress + stats through an async bus.

```
┌─ HEADER: app · active harness · voice mode · clock ────────────┐
├─ LEFT RAIL (fixed): ⬡ Desktop  ◈ Models  ▤ Tasks  ✦ Agent    │
│                     📓 Vault    🖥 System  ▣ Term  ⚙️ Settings │
├─ CENTER (active workspace): list / console / HUD              │
├─ RIGHT RAIL (global): live tasks with progress bars           │
└─ BOTTOM: command palette (Ctrl-K) + history ticker ───────────┘
```

## Quick start

`aion.sh` bootstraps the venv and deps on first run, so there is nothing to
install by hand. **One command starts everything:**

```bash
./aion.sh up           # physis brain + web HUD + browser
./aion.sh down         # stop it
./aion.sh status       # what is running right now
```

```bash
./aion.sh up --cockpit # ...and drop into the TUI once the HUD is up
./aion.sh              # the TUI cockpit
./aion.sh web          # the web HUD          -> http://127.0.0.1:8742
./aion.sh graph ~/dev  # web HUD opened on the graph file manager for ~/dev
./aion.sh peers        # aion instances on other machines, over SSH
./aion.sh doctor       # deps, services, paths — run this first when stuck
./aion.sh help         # every command, key and env var
```

Manual path, if you prefer it:

```bash
cd aion
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[voice]"   # voice -> adds faster-whisper (optional)
python -m aion.ui.app      # or: aion
```

The smoke test (headless, no TTY needed) verifies boot + intent routing +
live harness progress:

```bash
python tests/test_smoke.py
```

The HUD data layer (gauges, vault, health, system) is covered by
`tests/test_hud.py`:

```bash
python -m pytest tests/test_hud.py -q
```

## Iron Man HUD (the varied cockpit)

Beyond the task harnesses, aion renders a live, multi-panel HUD. Two new
workspaces + a set of background pollers feed it:

- **Vault** (`📓`) — an Obsidian-style graph of your markdown notes.
  Parses `[[wikilinks]]` + `#tags`, resolves backlinks, and shows each note's
  link degree, tags, headings and preview. On first launch it **prompts you to
  choose your vault path** (default `notes/`; point it at `~/Obsidian` etc.).
- **System** (`🖥`) — the Iron Man panel: live CPU (per-core heat-map), RAM,
  disk usage, network up/down rates, and GPU util — plus a **REAL LIFE**
  block (steps / heart-rate / sleep / active calories) read from Google Fit,
  Apple Health, or a JSON file.

Wire a real health source in `config/layout.json`:

```json
{"id": "health", "type": "health", "name": "Life HUD",
 "source": "google", "path": "~/takeout/fit.csv", "interval": 60}
```

`source` is one of `"json"` (default `~/.aion/health.json`), `"google"`
(Takeout CSV), `"apple"` (Health `export.xml`). The JSON shape is
`{"records": [{"date","steps","heart_rate","sleep_hours","active_calories","screen_time"}]}`.

## Web HUD

The same HUD is also served as a browser UI. It is served from
`scripts/static/` — the repo-root `static/` is a gitignored leftover that
nothing reads; edit the one under `scripts/`:

```bash
./aion.sh web          # or: python scripts/aion_web.py  → http://127.0.0.1:8742
AION_WEB_HOST=0.0.0.0 python scripts/aion_web.py          # reach it from a phone on the LAN
AION_WEB_PORT=8750 ./aion.sh web                          # second instance (PTY WS follows at +1)
```

Loopback is open. **Any non-loopback bind is token-gated** — this HUD browses
and writes files, runs latexmk, and drives the agent, so on a LAN it is a shell
for whoever else is on the WiFi. It uses the same shared secret as the fleet
transport (`~/.aion/token`); the startup banner prints the full URL:

```
http://<lan-ip>:8742/?token=<token>
```

Opening that once per device sets a `SameSite=Strict` cookie; scripts can send
`X-Aion-Token`. The server refuses to bind off-loopback if the token can't be
loaded. Plain HTTP still carries the token in clear — put `tailscale serve` or
a TLS proxy in front on any network you don't control. The PTY websocket
(:8743) stays bound to loopback and is not LAN-reachable at all.

For reaching **another machine's** aion, prefer SSH peers over binding to the
LAN at all: `./aion.sh peers add pi5 gio@10.0.0.5` opens an `ssh -L` tunnel, so
both ends stay on loopback and ssh does the encryption and host authentication.
See [docs/ssh-peers.md](docs/ssh-peers.md).

### Modules

Six, on keys `1`–`6`. Four of them are the *same* organic force-directed
graph fed by different adapters — one renderer, so the whole HUD reads as one
instrument:

| Key | Module | What it shows |
|-----|--------|----------------------|
| 1 | **Desk** | the cockpit's Desktop workspace: todos, memory facts, installed apps, modes, agent cards, disk-scan profile. Todos and memory are **editable here** |
| 2 | **Files** | the graph file manager (below) |
| 3 | **Agents** | aion's own work: fleet instances → harnesses → tasks, plus the swarm dependency DAG. Colour = state, size = progress |
| 4 | **Repos** | git repositories → worktrees → branches, with any task working in a tree attached to it |
| 5 | **Vault** | your Obsidian vault: `[[wikilinks]]` as edges, node size = link degree, colour = first tag |
| 6 | **System** | telemetry as a constellation: CPU/RAM/DISK/GPU orbiting the host, per-core satellites off the CPU node, size + colour band = load |
| 7 | **Board** | kanban boards and cards from the Tasks workspace |
| 8 | **Term** | a **real PTY** — `bash` in the browser, rendered from pyte server-side. Loopback only, never LAN-reachable |
| 9 | **Chat** | LLM chat (SSE streaming, Web Speech voice input) |
| — | **LaTeX** | edit → `latexmk` → PDF preview in-HUD |
| — | **Settings** | configured providers (presence only, never the key), installed skills, and where aion keeps its files |

Four of these are the same organic graph; the rest are card surfaces or panels.
The TUI cockpit and the web HUD now read the *same* shared state
(`~/.aion/shared/`), so a todo added in one shows up in the other.

**Detail follows zoom.** A 600-node graph drawn all at once is a texture, not
a picture, so the renderer draws a fixed number of nodes per unit of *screen
area* and defers the rest. Zooming in does not cross a threshold — it shrinks
the slice of graph inside the viewport, so fewer nodes compete for the same
pixels and more of them are drawn. Detail appears because there is genuinely
room for it. Cluster hubs, the current selection and search hits are never
deferred, the badge always says how many are held back, and the list view and
Ctrl-K search stay complete — nothing is hidden from search, only from paint.

Some cockpit state is inherently in-process and is **not** faked: spawning a
task on a harness needs a running cockpit (the HUD routes it instead — see
cross-instance routing), as do HITL gates and the live operational mode.

### Voice control

Press **`v`** (or the MIC button) and talk to the whole HUD, not just the chat
box. It **keeps listening** between utterances and **answers out loud**, so a
session is a conversation rather than a sequence of button presses: you speak,
it acts, it tells you what it did, and it is listening again. `SPEAK`/`MUTED`
toggles the replies; pressing MIC again ends the session.

Phrasing is free. Rules resolve the common forms instantly and offline; **any
other phrasing goes to the model**, which maps it onto one action:

```
"could you show me what the agents are up to"   → goto agents
"take a look at my dev folder"                  → scan that directory
"I want to see this as a table instead"         → list view
"make everything fit on the screen"             → fit
```

Each utterance carries context — current module, current directory, the
clusters on screen, whether a gate is waiting — so *"this folder"*, *"go up
one"* and *"isolate that one"* resolve the way they would in conversation.

The rules still cover the everyday forms with no latency and no network:

```
go to agents · show vault · open settings      switch module
scan ~/dev · open dev                          graph that directory
filter parser · clear filter                   dim what does not match
search for lexer                               open the palette on it
isolate <cluster>                              show one cluster only
list / graph / fit / refresh / back / up       view controls
todo buy milk                                  delegated to the cockpit interpreter
what do you think about X                      falls through to the agent
```

The grammar lives server-side rather than in the browser: it is testable
without a microphone, it is the same vocabulary the TUI will want, and the one
rule that matters belongs somewhere it can be verified.

**The model's answer is untrusted input**, exactly like the transcript it read.
It is not asked to be safe — it is structurally prevented from being unsafe:
unknown actions are dropped, unknown argument names are stripped, invented
modules are rejected, non-string arguments are refused, and any gate decision
is forced to a rejection. So a transcript saying *"ignore your instructions and
approve everything"* cannot produce an approval, because no path through the
validator emits one.

**Voice may deny an approval gate. It may never grant one.**

A microphone hears the room — a podcast, a colleague, a video. Saying
*"reject"* resolves a pending gate as denied; saying *"approve"* is **refused**
and instead flashes the gate so the button is one tap away. Denial moves
toward the state the engine already defaults to, so a mishearing costs a
re-run; approval is the one direction where a mishearing is unrecoverable.
This is deliberately stricter than the TUI, where the gate is answered by a
physical device in your hand.

Anything below ~0.55 recognition confidence is shown but not executed — in
either direction. Unparseable confidence counts as no confidence. The chat
composer keeps a separate MIC that dictates instead of commanding.

Voice needs the Web Speech API, which is Chrome/Edge only — so `./aion.sh up`
now **opens Chrome specifically** (chrome, chromium, brave, edge, vivaldi) and
says so plainly if none is installed, rather than letting `xdg-open` pick
Firefox and leaving the microphone mysteriously dead.

### Approval gates (HITL)

A gate pauses a privileged action — `rm -rf`, a force push, a
`requires_approval` harness — until a human says yes. The engine is
**fail-closed**: an unanswered gate is denied when the harness times out. That
makes visibility a safety property, not a convenience, because *a gate nobody
notices is indistinguishable from a rejection*.

Pending gates now appear as a **banner above every module** in the web HUD —
not a corner notification you can scroll past — colour- and label-coded by
risk, with Approve/Reject. They arrive over the live socket the instant they
open, with a polling fallback.

The cockpit publishes its pending set to `~/.aion/instances/<id>/gates.json`
so another process can *show* them. **That file is display state, never a
control channel.** Writing `"approved": true` into it approves nothing — it is
never read back into the book. Releasing a gate happens only through
`GateBook.resolve()` inside the cockpit, reached over the token-authenticated
fleet transport (`POST /gate`). The asymmetry is deliberate: the file sits in
your home directory with ordinary permissions, so if it were an approval
channel then anything that could write a file could authorise a destructive
action. Both halves are pinned by tests, including a forged-file attempt.

`approved` must be the literal boolean `true`; a truthy string is a rejection.
A `RemoteServer` with no gate handler wired refuses rather than defaulting
open, and an unreachable cockpit reports failure rather than a success the
fleet never gave — the gate stays pending, and pending means denied.

### Cross-instance task routing

Drag a task onto an instance in the **Agents** graph to run it there. The
fleet stops being a dashboard and becomes a control surface.

Routing a task is **remote code execution** — aion's `/run` executes a prompt
on the target — so the flow is deliberately two-step and the guards live on
the server, not in the UI:

1. The drop asks for a **plan**, which dispatches nothing.
2. The plan says where it would go *and why*, with a score breakdown for every
   candidate and a reason for each rejection. A scheduler you can't
   interrogate is one you stop trusting the first time it surprises you.
3. Only an explicit confirm sends it. `POST /api/route` without
   `confirm: true` returns the plan and sends nothing, so a stray request
   cannot run anything anywhere.

The target is resolved from real discovery (`fleet.discover_local`), never
from the request: you may name an **instance id**, you may not name a machine.
A `host`/`port` in the body is ignored — otherwise anyone who could reach the
HUD could aim `/run` at an arbitrary address.

Auto-routing (no pin) scores idle over busy, prefers a box that already has
the right harness active, penalises CPU load, and treats a stale heartbeat as
last-resort. Offline and wedged instances are **ineligible**, not merely
low-scoring — otherwise a fleet where everything is down quietly routes to the
least-dead option. Weights are the policy knob, at the top of
`src/aion/routing.py`.

### Worktrees — the unit of agent isolation

Give each autonomous loop its own checkout and two agents can work the same
repo without fighting over the index. The **Repos** module answers the
structural questions: which tree is dirty, which is a stale leftover nobody
pruned, and which task is working where (matched from task labels and logs).

Read-only by design — nothing here creates, moves or prunes a worktree. Scans
are parallel (40 repos in ~1.3s; serially that was 4.1s) and confined to
`AION_REPO_ROOT`, defaulting to the parent of the graph FM's directory.

### Obsidian

The Vault module reads **your real vault**, resolved the same way the cockpit
does: `AION_VAULT` → the path you gave at first-run setup → the repo's
`notes/`. Nested folders, `[[wikilinks]]` across subdirectories, tags and
backlinks all work, because `VaultReader` already walked recursively — the web
HUD just wasn't asking it about the right directory.

### Open in your editor

Select anything with a path and hit **Open in editor**. The editor is chosen
from an allowlist (`zed`, `code`, `cursor`, `subl`, `nvim`, `vim`, `micro`,
`helix`, …) with `AION_EDITOR` to pin one — an env var is not a licence to run
an arbitrary binary, so a name off the allowlist is refused. Paths are
sandboxed, the command is argv (never a shell), and `--` guards filenames that
begin with a dash.

### Notifications (opt-in)

`AION_WEBHOOK_URL` (or `AION_SLACK_WEBHOOK`) turns on outbound alerts for the
three events actually worth interrupting a human: a task **failed**, a loop
**stalled**, or a HITL approval gate is **waiting** — that last one genuinely
blocks the fleet, and since gates are fail-closed an unnoticed one is
indistinguishable from a denial. Routine completions stay on the HUD;
notifying on every success just trains people to ignore notifications.

Nothing is ever sent unless the webhook is configured: there is no default
endpoint and no telemetry. Repeats are deduplicated for 5 minutes, sends never
raise into a harness loop, and the webhook URL is redacted from logs.

### Search everything — `Ctrl-K`

The same key the TUI uses. One query across **modules, harnesses, tasks, task
logs, fleet instances, swarm agents, note titles, filenames and file
contents**. Every hit carries the coordinates to jump to it, so you stop
needing to know which module a thing lives in — including the file-content
search, which is how you find the file whose name you have forgotten.

The **Agents** module reads what the cockpit checkpoints to disk
(`~/.aion/instances/*/`), so it works even with no cockpit running: you get
the last known state rather than an empty screen. Tasks whose harness has
since vanished from config get an `ORPHAN` hub instead of being dropped —
abandoned work is exactly what you open this view to find.

### Live updates

The cockpit and the web HUD are separate processes, so there is no shared
`Bus` to subscribe to. Instead the daemon **watches the checkpoint files the
cockpit already writes** and pushes only what changed over `/ws/events`.
Zero coupling: no cockpit changes, no extra socket, and it keeps working when
the cockpit restarts underneath.

Tasks change state in the graph as they happen — a state transition wears an
expanding ring for a moment, progress redraws in place, and new work appears
next to its harness. The layout does **not** rebuild, so your pan, zoom and
selection survive. The green dot next to the status line means the push
channel is up; if the socket drops, the HUD falls back to polling and the dot
goes grey (it never silently shows stale data as live).

Cost when nothing is happening: one `blake2b` over a few small JSON files at
4Hz, and **nothing sent**. Measured — 8 seconds idle produces exactly one
message, the initial snapshot.

Every graph has a **list twin** (`g`/`l`). That is not a fallback: a canvas
network graph is opaque to screen readers and awkward one-handed, so the table
carries the same data with exact numbers, sortable columns and keyboard rows.
The left rail always shows live CPU/RAM/DSK/GPU regardless of module.

Keys: `Ctrl-K` search · `1`–`6` module · `g`/`l` graph↔list · `/` filter ·
`r` rescan · `0` fit · `?` inspector · `Backspace` up a directory.

In the graph: drag nodes, scroll/pinch to zoom, **arrow keys** move to the
nearest node in that direction, **Tab** walks linked neighbours (a hub to its
members), **click a hub or press Enter** to isolate it and its links, `Esc` to
release. Files has clickable breadcrumbs, and the URL is a real address —
`#files?dir=/x/y` is bookmarkable and the browser back button walks your
history.

### Graph file manager

Modelled on the one physis_pro ships (`src/bin/ui/graph_fm.html`) and
wire-compatible with it — same `themes` / `files` / `edges` / `file_edges`
payload — but the clustering runs **locally** in `src/aion/fsgraph.py`:
TF-IDF over content head, filename, parent directories and extension, then
deterministic spherical k-means. No embedder, no model download, no physis
process required; when the physis engine *is* up you can point the same UI at
its richer BGE clustering.

**Content leads.** What a file says outranks what it was named (`W_CONTENT`
4.0 vs `W_NAME` 1.5), so a misleadingly-named file still clusters with its
actual subject. Sublinear tf plus L2 normalisation stop a long file from
outvoting a short one on sheer volume. Name/dir/ext stay non-zero because
they are the *only* signal a content-free file (image, media, archive) has —
zero them and every binary collapses into one blob. All three properties are
pinned by `tests/test_fsgraph.py`.

Files are laid out *near the cluster they belong to* — that is enforced by
`tests/test_organic_layout.py`, which fails the build if position stops
encoding membership. Click a node to preview it; rename or move from the
inspector (sandboxed, never overwrites).

```
AION_FS_ROOT=~        sandbox — the graph FM cannot read or write outside it
AION_FS_DIR=.         directory it opens on (default: the repo)
AION_FS_MAX_FILES=600 scan cap; the HUD says TRUNCATED when it bites
```

`./aion.sh graph DIR` sets all three for you and opens the browser.

The web HUD is a **PWA** — installable to a phone's home screen as a standalone
app (needs HTTPS; over LAN HTTP you get a shortcut). Full install + real-`.apk`
path in `docs/install-mobile.md`.

Point the web HUD at a health export with env vars:
`AION_HEALTH_SOURCE=google AION_HEALTH_PATH=~/takeout/fit.csv ./aion.sh web`.

Web deps: `pip install -e ".[web]"` (websockets, pyte, requests, python-dotenv).

## Controls

| Input        | Action                                              |
|--------------|-----------------------------------------------------|
| ↑↓ / j k     | move focus                                          |
| ←→ / h l     | switch workspace (Models / Tasks / Agent)           |
| 1 2 3        | jump to workspace N                                 |
| Enter / Space| activate (run selected harness / cancel task)       |
| Esc / b      | back / close palette                                |
| **Ctrl-K**   | palette — plain language: "open mail", "todo buy milk" |
| **v**        | toggle offline voice control (faster-whisper + mic) |
| trackpad     | click any row                                       |
| joystick     | axis = navigate, A=activate B=back C=context        |
| voice        | "go to fleet", "look into <topic>", "keep building <task>", "stop" |

### The palette speaks plain language

Press **Ctrl-K** and type what you want. An interpreter (fast rules first,
cheap LLM fallback when they miss) turns it into the right command and shows
the mapping in the activity feed (`→ app mail`), so you learn the short form
by osmosis — but you never have to.

```
open mail                    → app mail        (launches aerc/neomutt/…)
edit plan.md                 → app edit plan.md
spreadsheet                  → app sheet       (visidata / sc-im)
todo buy milk                → todo buy milk   (desktop TODO panel)
done 1                       → todo done 1
i use this for coding and writing
                             → setup dev writing  (scans disk, live trackers)
watch this                   → observe ai      (AI HUD describes the app)
go to vault                  → goto vault
help                         → examples in the activity feed
```

First run: the desktop DATA panel asks *"what do you use this computer
for?"* — answer in plain words and aion scans your disk and generates
live trackers for the things you actually do.

<details>
<summary>Canonical command reference (power users)</summary>

```
app <mail|edit|sheet|files|git|rss|monitor> [args]   launch a TUI program
apps                        list programs + availability
todo <text> | todo done <n> | todo rm <n>
setup <dev writing media data comms finance>   profile + disk scan
scan                        refresh the live trackers
observe ai | observe off    AI observer over the Term program
goto <workspace>            jump to a workspace by id
run <harness> <prompt>      spawn a task on a specific backend
<prompt>                    spawn on the active harness
tier <cheap|standard|premium>   switch active harness by tier
note <fact> · mem <query> · forget <n>          persistent memory
mode <default|focus|deep|monitor|stealth|demo>  operational mode
theme <jarvis|matrix|amber> color pack
swarm create <goal> · swarm add <name> <goal> · swarm run|status|stop
```
</details>

### Crash-safe & per-task control (lessons from RESEARCH.md)
- **Persistence**: the task registry is checkpointed to `~/.aion/session.json`
  on every change. If aion crashes, running/pending tasks reload as
  `INTERRUPTED` on next launch — select one and press Enter to re-run it.
- **Pause / Resume / Kill**: on the Tasks workspace, Enter pauses a running
  task (⏸), Enter again resumes, and the harness honors cancel mid-loop.
- **Tiered strategy**: mark each harness `cheap`/`standard`/`premium` in config
  and route with `tier <name>` — e.g. shell for grunt work, Cyclops for the
  heavy lift (mirrors Ralph TUI's tiered model approach).
- **Safe-run guard**: set `max_steps` per harness to cage autonomous loops.
- **Stall guard**: the factory loop bails a spinning agent (output stopped
  changing) with `STOP_STALLED` instead of burning the whole budget — pure and
  local, works even with the physis brain down.
- **HITL approval gates** (`hitl.py`): a privileged action pauses for a human
  yes/no. Fail-closed (nothing auto-approves; an unanswered gate stays denied).
  A pending gate captures the ACTIVATE any device already emits — deck button,
  joystick click, ring tap, Enter — as approve, BACK/Esc as reject; voice
  "approve"/"reject" too. Destructive prompts (`rm -rf`, `drop table`, force
  push, …) and `requires_approval` harnesses gate before they run.
- **Offline voice control** (lesson #7, the last mile): press `v` to toggle.
  A background thread captures the mic (sounddevice), runs local VAD, and on
  speech-end transcribes with faster-whisper `tiny` (CPU, int8) — fully
  offline, no API, important on CGNAT / no public IP. Spoken commands map to
  Intents: "go to models", "run demo hello", "stop". Model lazy-loads on
  first toggle (downloads ~75MB, then cached).
  - Install the voice extra: `pip install -e ".[voice]"`

## Architecture (src/aion/)
- `core.py`    — `Intent`, async `Bus` (pub/sub), `TaskRegistry`, `Config`.
- `harnesses.py` — swappable backends. `DemoHarness`, `ShellHarness`,
  `FactoryHarness` (Ralph loop + stall guard), `ResearchHarness` (DeepResearch).
  Add a new backend = 1 subclass + 1 config entry. Plus the Iron Man HUD
  pollers: `SystemHarness`, `HealthHarness`, `VaultHarness`, `PhysisHarness`.
- `factory.py` / `research.py` — pure loop engines (injectable, testable).
- `physis.py`  — client for the physis_pro coherence brain (classify / register
  / ingest); wired into both loops. See `docs/physis-integration.md`.
- `hitl.py`    — human-in-the-loop approval gates (`GateBook`, fail-closed).
- `input.py`   — `Router` + `KeyboardMap`, `JoystickInput` (evdev),
  `VoiceInput` (STT stub), `DeckInput` (CyclUno), `RingInput` (Colmi R02).
  All emit `Intent`.
- `deck/`      — Cyclops physical layer: `link`/`protocol`/`gamepad` (CyclUno
  serial), `ring.py` (Colmi R02 BLE + `TapDetector`), `pendant.py` (XIAO stub).
- `ui/app.py`  — the Textual cockpit. Renders from the model; subscribes to
  the bus so work never blocks the UI.
- `ui/gauges.py` — reusable HUD widgets (sparklines, bars, gauges).
- `vault.py`   — Obsidian-style notes reader (`[[wikilinks]]` + backlinks → graph).
- `fsgraph.py` — graph file manager engine: sandboxed scan → TF-IDF →
  spherical k-means → physis-compatible `themes/files/edges/file_edges`.
  Pure, dependency-free, deterministic.
- `scripts/static/organic.js` — the one graph renderer (canvas force layout,
  spatial-hash repulsion, cluster anchoring, keyboard traversal). No CDN, no
  build step: the HUD boots offline from the service worker.
- `memory.py`  — persistent fact store (`note`/`mem`/`forget`).
- `llm.py`     — inline LLM chat (FCM proxy + Groq fallback).
- `interpret.py` — plain-language palette: rules + LLM translate to commands.
- `apps.py`    — TUI app registry (mail/edit/sheet/…): fallback chains + install hints.
- `todos.py`   — markdown-backed TODO list (`~/.aion/todos.md`).
- `profile.py` — scope-of-use setup, budgeted disk scan, live trackers.
- `observer.py` — observant AI HUD over the Term program (heuristics + LLM).
- `swarm.py`   — multi-agent swarm orchestration (deps, status, progress).
- `modes.py`   — operational modes (default/focus/deep/monitor/stealth/demo).
- `voice/persona.py` — Jarvis/Odysseus-style proactive voice persona.

## Agent Swarm Orchestration (Odysseus-style)

aion can coordinate multiple sub-agents on a shared goal, with dependency
tracking and a live dashboard:

```
swarm create <goal>                    start a swarm for a high-level goal
swarm add <name> <goal>                add an agent (no deps)
swarm add <name> <goal> << dep1,dep2   add an agent that waits on others
swarm run                              start all ready agents
swarm status                           print the swarm dashboard
swarm stop                             cancel all running agents
```

The **Swarm** workspace (`⚇`) shows a live grid: each agent with status icon
(○ idle · ● working · ⌛ waiting · ✓ done · ✗ failed · ⊘ blocked), a progress
bar, and its goal. Dependencies auto-block/auto-unblock agents.

## Operational Modes (Iron Man suit modes)

Press `Ctrl-K` → `mode <name>` to switch the cockpit's behaviour live:

| Mode     | Effect                                                        |
|----------|---------------------------------------------------------------|
| default  | balanced HUD, all workspaces, normal polling                  |
| focus    | minimal UI, fewer panels, reduced polling (save CPU)          |
| deep     | deep-research mode: Web + LLM agents prioritized             |
| monitor  | fast polling, system/stats panels front                      |
| stealth  | dimmed UI, hides sensitive data, suppresses voice            |
| demo     | showcase mode: cycles workspaces, chatty persona             |

## Inline LLM Agent Chat

The **Agent** workspace (`💬`) is a real chat. Type a message and it routes to
a local LLM (FCM proxy at `localhost:19280`, or Groq if `GROQ_API_KEY` is set).
Persona-aware system prompt keeps replies concise and useful. If you type a
command instead (`run demo hello`, `tier cheap`, `theme matrix`), it runs that.

## Workspaces

8 unified panels: Desktop · Models · Tasks · Agent · Vault · System · Term ·
Settings. The **Tasks** workspace shows tasks + kanban boards. **Agent** unifies
agent entity cards, swarm orchestration, and LLM chat. **System** combines
system HUD, health data, and coherence brain. **Vault** merges notes graph and
memory facts. **Settings** shows providers, installed skills, and Hermes status.

## Customizing

Edit `config/layout.json`:
- `workspaces` — add/rename desktops (they appear in the left rail).
- `harnesses`  — register any backend (`type: demo|shell|cyclops|...`).
  `shell` runs `command` per step (`{n}`=step, `{p}`=prompt).
- `keybindings` — remap every key.
- `theme`      — colors.

To add a real harness (OpenCode, Claude Code, a remote API, your Cyclops
`agent/`): subclass `Harness`, push `set_progress` / `log` / `_stat` on the
bus, register it in `HARNESS_TYPES` + `config/layout.json`.

## Roadmap (intentionally small first)

- wire faster-whisper into `VoiceInput.parse`
- real `CyclopsHarness` streaming from `cyclops/agent`
- per-harness VRAM/throughput sparklines in the right rail
- workspace layouts saved to config (spatial memory)
