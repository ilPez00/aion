# aion

A customizable **multi-harness AI cockpit** and **stats visualizer**, driven by
a TUI you control with keyboard, trackpad, a joystick/gamepad, voice — and the
**CyclUno deck**, a physical console (2 sticks + clickable wheel + buttons +
OLED on an Arduino Uno) that navigates the cockpit one-handed and doubles as
a Linux gamepad for programs aion spawns. See `docs/DECK.md`.

The core idea: every input device emits the *same* `Intent` objects, and every
AI backend is a swappable `Harness`. The UI only renders; harnesses push live
task progress + stats through an async bus. That's what makes "joystick, voice,
keyboard, mouse all drive one screen" trivial instead of four code paths.

```
┌─ HEADER: app · active harness · voice mode · clock ────────────┐
├─ LEFT RAIL (fixed):  ◈ Models  ▤ Tasks  ✦ Agent               │
├─ CENTER (active workspace): list / console                    │
├─ RIGHT RAIL (global): live tasks with progress bars           │
└─ BOTTOM: command palette (Ctrl-K) + history ticker ───────────┘
```

## Quick start

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

## Controls

| Input        | Action                                              |
|--------------|-----------------------------------------------------|
| ↑↓ / j k     | move focus                                          |
| ←→ / h l     | switch workspace (Models / Tasks / Agent)           |
| 1 2 3        | jump to workspace N                                 |
| Enter / Space| activate (run selected harness / cancel task)       |
| Esc / b      | back / close palette                                |
| **Ctrl-K**   | command palette: `run <harness> <prompt>`           |
| **v**        | toggle offline voice control (faster-whisper + mic) |
| trackpad     | click any row                                       |
| joystick     | axis = navigate, A=activate B=back C=context        |
| voice        | "go to models", "run demo hello", "stop"            |

### Command palette grammar
```
run <harness> <prompt>      spawn a task on a specific backend
<prompt>                    spawn on the active harness
tier <cheap|standard|premium>   switch active harness by tier (lesson #3)
app <program>               spawn a real program (pause=SIGSTOP, cancel=kill)
note <fact>                 remember something (persistent memory)
mem <query>                 recall — opens the Memory workspace filtered
forget <n>                  drop fact #n from the current memory view
```

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
- **Offline voice control** (lesson #7, the last mile): press `v` to toggle.
  A background thread captures the mic (sounddevice), runs local VAD, and on
  speech-end transcribes with faster-whisper `tiny` (CPU, int8) — fully
  offline, no API, important on CGNAT / no public IP. Spoken commands map to
  Intents: "go to models", "run demo hello", "stop". Model lazy-loads on
  first toggle (downloads ~75MB, then cached).
  - Install the voice extra: `pip install -e ".[voice]"`

## Architecture (src/aion/)
## Architecture (src/aion/)
- `core.py`    — `Intent`, async `Bus` (pub/sub), `TaskRegistry`, `Config`.
- `harnesses.py` — swappable backends. `DemoHarness`, `ShellHarness`,
  `CyclopsHarness` (stub). Add a new backend = 1 subclass + 1 config entry.
  Plus the Iron Man HUD pollers: `SystemHarness` (computer stats),
  `HealthHarness` (real-life stats), `VaultHarness` (notes graph).
- `input.py`   — `Router` + `KeyboardMap`, `JoystickInput` (evdev),
  `VoiceInput` (STT stub). All emit `Intent`.
- `ui/app.py`  — the Textual cockpit. Renders from the model; subscribes to
  the bus so work never blocks the UI.
- `ui/gauges.py` — reusable HUD widgets (sparklines, bars, gauges).
- `vault.py`   — Obsidian-style notes reader (`[[wikilinks]]` + backlinks → graph).
- `health.py`  — pluggable real-life stats (Google Fit / Apple Health / JSON).
- `sysinfo.py` — computer stats reader (CPU/RAM/disk/net/GPU via psutil).

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
