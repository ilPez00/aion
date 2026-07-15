# aion — Implementation Plan

## Completed

### Phase 0: Foundation (original 3-workspace minimal)
- ShellHarness, DemoHarness, CyclopsHarness, RemoteHarness stubs
- Bus/Store/Intent architecture
- Keyboard + joystick input
- Memory workspace
- 23/30 tests passing (baseline)

### Phase 1: Voice + Personality
- Edge-TTS voice output with pyttsx3/espeak fallback
- Persona system (10+ templates, 3 verbosity × 2 formality, persist)
- Jarvis/Matrix/Amber theme presets
- TTS on task events, greeting on boot
- 30/30 tests (fixed 7 pre-existing failures)

### Phase 1b: Hermes Integration
- `HermesClient` (async CLI wrapper for `hermes chat/kanban/memory/skills`)
- `KanbanReader` (read-only SQLite, 20 tasks)
- `HermesMemoryReader` (parses MEMORY.md, 10 sections)
- `SkillLoader` (scans 319 skills from 2 directories)
- `GatewayBridge` (gateway status)
- `HermesHarness` (run prompt through hermes CLI)
- `SkillHarness` (load + step through SKILL.md workflow)
- Hermes and Skills TUI workspaces
- 2 new bus topics, store subscriptions, renderers

### Phase 1c: Settings + .env Reader
- `env.py` parser: reads ~/.env, 341 entries across 22 categories
- Settings workspace (⚙️) showing providers with key status, masked previews, endpoint indicators
- Backend health inferred from key presence
- 12 tests

### Phase 1d: OpenCode Harness
- `OpenCodeHarness`: spawns `opencode run -m <model> --auto`, streams stdout to task log
- Full lifecycle: pause (SIGSTOP), resume (SIGCONT), cancel (SIGTERM→SIGKILL)
- Registered in HARNESS_TYPES + layout.json
- 8 tests (FakeProc-based lifecycle coverage)

## Planned / In Progress

### Phase 2: Hardening & Polish  ← CURRENT

Priority order:

| # | Issue | File | Effort | Status |
|---|-------|------|--------|--------|
| 1 | `--auto` hardcoded — security risk | `harnesses.py` | 1 line + config | PENDING |
| 2 | No timeout — task can hang forever | `harnesses.py` | ~10 lines | PENDING |
| 3 | ANSI stripping lossy (isprintable strips tabs/emoji/unicode) | `harnesses.py` | 1 regex | PENDING |
| 4 | `_read_output` races with main lifecycle loop | `harnesses.py` | restructure ~20 lines | PENDING |
| 5 | Config model: model/binary in untyped `extra` dict | `harnesses.py` + `core.py` | HarnessConfig fields | PENDING |
| 6 | No OpenCodeClient abstraction | `hermes/opencode.py` + `harnesses.py` | new file + refactor | PENDING |
| 7a | No boot progress feedback | `harnesses.py` | ~10 lines | PENDING |
| 7b | Stderr discarded during execution | `harnesses.py` | ~15 lines | PENDING |
| 7c | `load_env_sync()` uses fragile `__dict__` | `env.py` | dataclasses.asdict | PENDING |
| 7d | Settings workspace flashes blank on first render | `store.py` + `app.py` | pre-load or loading state | PENDING |
| 8 | Direct `_read_output` tests | `test_opencode.py` | new test cases | PENDING |

### Phase 3: Fallback Cascade Router
- `FallbackRouter` in new `src/aion/router.py`
- Per-task fallback chain: Hermes → OpenCode → Ollama
- Circuit breaker: disable failing providers for N seconds
- Cost tracking per provider
- TUI right rail shows cumulative spend

### Phase 4: Provider Config for Hermes
- `src/aion/hermes/providers.py` — generate Hermes YAML from .env
- Push via `hermes config set` or write `~/.hermes/config.yaml`
- Add missing providers: Mistral, xAI, Together, Cohere, SambaNova, etc.
- Round-robin Gemini keys (20+)
- Configure free proxies as Hermes backends

### Phase 5: Health Checks
- Background poller pings each provider's `{base_url}/models`
- Latency + status per provider
- TUI right rail shows live provider health

### Phase 6: DeepSeek v4 Free as Hermes Backend
- Provider entry for deepseek-openrouter via minimax endpoint
- Configurable from settings panel
