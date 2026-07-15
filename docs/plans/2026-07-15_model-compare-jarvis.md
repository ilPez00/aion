# Multi-Model Compare + Proactive Jarvis — Implementation Plan

> **For Hermes:** Execute in aion repo (`/home/gio/aion`). Factory-loop: build → test → commit per cycle.

**Goal:** Add (1) a side-by-side multi-model comparison workspace and (2) a proactive "Jarvis" layer that watches state and surfaces suggestions — both inspired by the talon_hud/jarvis_ai research saved in `docs/reference/talon-voice.md`.

**Architecture:**
- `llm.py` gains `chat_send_multi(prompt, providers)` → `{provider: reply}` (provider-agnostic, wraps existing `_fcm_chat`/`_groq_chat`).
- `store.py` gains a `compare` intent handler + `state.compare_result` holding the side-by-side; Agent workspace renders two columns when a compare result exists.
- A new `jarvis.py` module produces proactive suggestions from `ViewState` (pure function, unit-testable). A background poller in the app calls it every N seconds and appends to the activity feed + a `state.suggestions` list.

**Tech Stack:** Python 3.11, Textual (existing), pytest, Rich markup (existing).

---

### Task 1: `chat_send_multi` in llm.py (TDD)
- **Files:** `src/aion/llm.py`, `tests/test_llm.py` (new)
- **Step 1 (test):** write `test_chat_send_multi_calls_providers` — monkeypatch `_fcm_chat`→`"A"`, `_groq_chat`→`"B"`, assert `chat_send_multi("hi", ["fcm","groq"]) == {"fcm":"A","groq":"B"}`.
- **Step 2:** run → FAIL (function missing)
- **Step 3:** implement `chat_send_multi(prompt, providers)` iterating providers, calling the matching backend, returning dict. Cap each to 400 chars.
- **Step 4:** run → PASS
- **Step 5:** commit `feat(llm): add chat_send_multi for side-by-side model comparison`

### Task 2: Compare intent + state
- **Files:** `src/aion/core.py` (IntentType.COMPARE + Intent.compare helper), `src/aion/store.py`
- **Step 1 (test):** `test_store_compare` — publish `Intent.compare("explain recursion")` → after await, `state.compare_result` has `prompt` + empty/partial `answers`; simulate providers returning → `answers` populated.
- **Step 2:** add `IntentType.COMPARE`, `Intent.compare(text)`.
- **Step 3:** `Store.handle` routes COMPARE → `asyncio.create_task(self._run_compare(text))` which calls `chat_send_multi` and stores result in `state.compare_result`.
- **Step 4:** run → PASS, commit `feat(store): add compare intent + compare_result state`

### Task 3: Agent workspace renders side-by-side
- **Files:** `src/aion/ui/app.py` (`_agent_panel`), `src/aion/llm.py` (format compare)
- **Step 1 (test):** `test_agent_panel_compare` — set `state.compare_result` with 2 answers, call `_agent_panel`, assert both provider labels + first 40 chars present, no markup errors.
- **Step 2:** in `_agent_panel`, if `store.state.compare_result` and not empty, render two columns (provider | reply) instead of conversation.
- **Step 3:** run → PASS, commit `feat(ui): render multi-model compare side-by-side in Agent workspace`

### Task 4: Proactive Jarvis suggestions (pure)
- **Files:** `src/aion/jarvis.py` (new), `tests/test_jarvis.py` (new)
- **Step 1 (test):** `test_jarvis_flags_failed_tasks` — ViewState with 3 failed tasks → suggestions contains "rerun". `test_jarvis_flags_high_cpu` — cpu 92 → contains "CPU". `test_jarvis_idle` — clean state → `[]` or low-priority only.
- **Step 2:** implement `suggest(state, cfg) -> list[str]` checking: failed tasks, high cpu/ram/disk, vault notes > 10 (index建议), swarm blocked agents, no active tasks (suggest demo).
- **Step 3:** run → PASS, commit `feat(jarvis): proactive suggestion engine (pure, testable)`

### Task 5: Wire Jarvis poller into app
- **Files:** `src/aion/ui/app.py` (`_tick` or new poll), `src/aion/store.py` (`state.suggestions`)
- **Step 1 (test):** `test_jarvis_poller_updates_state` — drive store with failed task, call `_poll_jarvis()`, assert `state.suggestions` non-empty and activity feed got a line.
- **Step 2:** add `state.suggestions: list[str]`; every ~10s call `suggest()` and prepend top suggestion to `state.logs` (activity feed) + set `state.suggestions`.
- **Step 3:** run → PASS, commit `feat(ui): wire proactive Jarvis poller into desktop activity feed`

### Task 6: Auto-improve + verify
- Run full suite `pytest tests/ -q`; fix any regression (target 73→75 green, pty test pre-existing).
- Self-review diff for dead code / missing tests. Commit `chore: auto-improve after model-compare + jarvis cycles`.
- Update `docs/RESEARCH.md` note linking talon_voice sources.

---

**Risks:** network LLM calls may be unavailable in CI — all tests mock backends, no live calls. Keep `chat_send_multi` non-blocking friendly (thread). Don't break existing 73/74.

**Verify for real:** `cd /home/gio/aion && .venv/bin/python -m pytest tests/ -q` + headless boot `TERM=xterm-256color timeout 6 .venv/bin/python -m aion.ui.app`.
