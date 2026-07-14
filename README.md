# aion

A customizable **multi-harness AI cockpit** and **stats visualizer**, driven by
a TUI you control with keyboard, trackpad, a joystick/gamepad, and voice.

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

- `core.py`    — `Intent`, async `Bus` (pub/sub), `TaskRegistry`, `Config`.
- `harnesses.py` — swappable backends. `DemoHarness`, `ShellHarness`,
  `CyclopsHarness` (stub). Add a new backend = 1 subclass + 1 config entry.
- `input.py`   — `Router` + `KeyboardMap`, `JoystickInput` (evdev),
  `VoiceInput` (STT stub). All emit `Intent`.
- `ui/app.py`  — the Textual cockpit. Renders from the model; subscribes to
  the bus so work never blocks the UI.

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
