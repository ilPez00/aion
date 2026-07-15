# aion — Iron Man / Jarvis HUD Expansion Plan

**Goal:** Build an informative, varied Iron Man / Jarvis-style HUD inside `aion`
that integrates multiple live data sources into one cohesive multi-workspace cockpit:

1. **Obsidian-style vault visualization** for notes (graph + backlinks)
2. **Agent progress** tracking (richer than the current progress bars)
3. **Real-life statistics** (health / fitness / sleep / screen-time)
4. **Computer statistics** (CPU / RAM / disk / network / GPU)
5. ...and a varied set of HUD widgets (gauges, sparklines, mini-charts) to present it all.

---

## Investigation summary (what already exists)

| Feature | Where | Status |
|---|---|---|
| Notes vault (markdown + `[[wikilinks]]`) | `notes/` dir, served by `server.py` (`/api/notes`) as graph | ✅ web-only graph API exists |
| Memory facts | `src/aion/memory.py` → `~/.aion/memory.json` | ✅ TUI workspace exists |
| Web system stats | `server.py` `system_stats()` — CPU/RAM/disk/net via `psutil` | ✅ web-only |
| GPU telemetry | `TelemetryHarness` polls `nvidia-smi` / `ollama` | ✅ poller + minimal display |
| Token / agent stats | `StatsHarness` + `stats.py` reads Hermes `state.db` | ✅ poller + rich display |
| Task progress | `TaskRegistry` + progress bars in right rail | ✅ core feature |
| Multi-workspace layout | Models / Tasks / Agent / Memory / Hermes / Skills | ✅ core architecture |
| Live task sparklines / vault graph in TUI | — | ❌ missing |

**Key files:**
- `src/aion/stats.py` — token/agent stats reader (add system stats reader)
- `src/aion/harnesses.py` — pollers (`StatsHarness`, `TelemetryHarness`); add `SystemHarness`, `HealthHarness`, `VaultHarness`
- `src/aion/store.py` — state + intent routing; add workspace item types
- `src/aion/ui/app.py` — TUI rendering; add workspace renders + gauges
- `src/aion/memory.py` — fact memory; add Obsidian vault reader
- `config/layout.json` — workspaces + themes; add new workspaces
- `server.py` / `static/index.html` — web HUD; mirror new data (optional)
- `tests/` — one file per new reader

---

## Design decisions (defaults — adjust if wrong)

1. **Vault location:** use the existing `aion/notes/` directory (already `[[wikilink]]`-compatible). Path configurable in `config/layout.json`. **On first run / config, prompt the user to set up their storage location** (default `notes/`, but allow pointing at a real `~/Obsidian/` vault).
2. **Health source:** pluggable reader supporting **Google Fit** and **Apple Health** exports (plus a generic JSON fallback). Start with a JSON adapter, then Google/Apple importers. Fields: `steps`, `heart_rate`, `sleep_hours`, `active_calories`, `screen_time`.
3. **Build order (my call):** gauges lib → computer stats → vault graph → health → agent progress → web sync. Gauges first because every visual panel depends on them.

---

## Implementation steps

### Step 1 — Unified computer statistics (`SystemHarness`)
- **Files:** `src/aion/harnesses.py` (new `SystemHarness`), `src/aion/core.py` (reuse `TOPIC_STATS`), `config/layout.json` (add `system` workspace + harness), `pyproject.toml` (add `psutil>=6`).
- **Change:** poll CPU (per-core %, load avg, temp), RAM (total/used/%, swap), disk (per-mount usage + IO), network (up/down bytes, conns), GPU (reuse `TelemetryHarness` logic). Publish structured dict under harness id `system` on `TOPIC_STATS`.
- **Verify:** `tests/test_system_stats.py` (5+ unit tests, mock `psutil`).

### Step 2 — Obsidian vault reader + TUI visualizer
- **Files:** `src/aion/vault.py` (new — scan `notes/*.md`, parse `[[wikilinks]]` → graph), `src/aion/harnesses.py` (`VaultHarness`), `src/aion/store.py` (`_current_items()` add `"vault"` case), `src/aion/ui/app.py` (`_center_line()` add vault render), `config/layout.json` (add `vault` workspace).
- **Change:** `VaultReader` builds node+edge graph; render navigable list (note, backlink count, preview); open note content in center panel. Defer full canvas graph to Step 2b (web already has one).
- **Verify:** `python -c "from aion.vault import VaultReader; print(len(VaultReader('notes/').graph()['nodes']))"`.

### Step 3 — Real-life statistics (`HealthHarness`)
- **Files:** `src/aion/health.py` (new reader), `src/aion/harnesses.py` (`HealthHarness`), `src/aion/store.py` (`_current_items()` add `"health"` case), `src/aion/ui/app.py` (health render), `config/layout.json` (add `health` workspace).
- **Change:** `HealthReader` reads `~/.aion/health.json`; render gauges + trend sparklines. Pluggable source (JSON → CSV → Cyclops relay).
- **Verify:** `tests/test_health.py` with mock data.

### Step 4 — Rich HUD widgets (gauges, sparklines, mini-charts)
- **Files:** `src/aion/ui/gauges.py` (new ASCII gauge lib), `src/aion/ui/app.py` (use in rails/center), `tests/test_gauges.py`.
- **Change:** reusable components — horizontal bar gauge (extend `bar()`), vertical stack gauge (multi-core CPU), mini line sparkline (`▁▂▃▄▅▆▇█`), dual-value gauge (used/total). Add sparkline history to task progress.

### Step 5 — Enhanced agent progress (timeline + history)
- **Files:** `src/aion/ui/app.py` (`_render_right()`, `_render_center()`), `src/aion/store.py` (track task history).
- **Change:** task timeline (timestamps per state change), recent task history log, sparkline of active tasks over time.

### Step 6 — Web HUD sync (optional)
- **Files:** `server.py` (new endpoints for system/health/vault), `static/index.html` (new panels).
- **Change:** mirror new sources into web HUD so TUI + web show the same data.

---

## Risks & mitigations
- **TUI graph viz is hard** (no built-in graph widget). → Start with tree/list + backlink counts; full graph deferred to web canvas.
- **Health source unknown.** → Pluggable `HealthReader`, JSON-first.
- **`psutil` not in `pyproject.toml`.** → Add `psutil>=6`; degrade gracefully if absent.
- **Crowded TUI.** → Tabbed panels per workspace; keep each focused.

---

## Test strategy
- New: `tests/test_vault.py`, `tests/test_health.py`, `tests/test_gauges.py`, `tests/test_system_stats.py`.
- Existing (must stay green): `test_smoke.py`, `test_store.py`, `test_stats.py`, `test_pipeline.py`.
- Manual: boot TUI, switch to new workspaces, confirm live data; run `python server.py`, open `http://127.0.0.1:8742`.

---

## Decisions locked (2026-07-15)
- Q1 Vault path → `notes/` default, configurable, **user prompted to set up storage at config time**.
- Q2 Health source → **Google / Apple** pluggable (generic JSON fallback).
- Q3 Build order → **gauges → computer stats → vault → health → agent progress → web sync**.

## Status
- [x] Plan written + decisions locked
- [ ] Step 4: gauges library
- [ ] Step 1: computer stats (SystemHarness)
- [ ] Step 2: vault reader + visualizer
- [ ] Step 3: health harness (Google/Apple)
- [ ] Step 5: agent progress enhancement
- [ ] Step 6: web HUD sync (optional)
