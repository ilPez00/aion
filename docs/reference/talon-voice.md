# Talon Voice — Research Compendium

Sources for this AI's HUD / voice / agent design.

## 1. chaosparrot/talon_hud (Unofficial Talon Head Up Display)

- **URL**: <https://github.com/chaosparrot/talon_hud>
- **License**: Not stated (README says "unofficial", fork-friendly)
- **Key ideas**:
  - Gaming-inspired HUD overlay for dictation/voice-control state
  - **Speech history** — rolling event log of commands said, auto-clear after N seconds, freeze for pair sessions
  - **Status bar** — mode indicator (sleep/awake/command/dictation), mic mute, language, code language, focus indicator
  - **Walkthrough system** — step-by-step interactive guides for learning voice commands
  - **Content toolkit** — browsable docs + debugging (scope, speech, list debug)
  - **Focus tracking** — orange box overlay on focused window
  - **Inactivity hiding** — auto-hide when fullscreen video, auto-show on speech
  - **Keyboard nav** — Tab/Space/arrows/Enter, `head up focus` / `head up blur`
  - **Environments** — layout per-context (browser vs IDE vs terminal)
  - **Three persona design** — User (prefs), Scripter (content), Themer (look)
  - **WYSIWYS voice** — "What You See Is What You Say": visible text = voice command to activate it
- **Widgets**: status bar, event log, choice panel, context menu, screen overlay, eye tracker content, sticky content
- **Config**: themes.csv (HEX colors), preferences folder, images in `_base_theme`

## 2. C-Loftus/talon-ai-tools

- **URL**: <https://github.com/C-Loftus/talon-ai-tools>
- **License**: MIT
- **Key ideas**:
  - Query LLMs / AI tools via voice commands
  - Multi-provider support (OpenAI, Anthropic, local models)
  - Models configuration via `models.json` — per-model API options, system prompt, temperature, model ID aliasing
  - Context **threads** — `{user.model} start thread:` for conversation follow-ups
  - Dictation **fix** — voice command to rephrase/rewrite selected text using LLM
  - Strip markdown from responses that weren't asked for
  - Action context — pass current selection, clipboard, or user-defined context into prompt
- **Architecture**: Python+Talon scripts, per-app contexts, streaming output
- **Voice commands**: "GPT ask", "GPT format", "GPT rewrite", "GPT continue"

## 3. hortocam/jarvis_ai (MIT, already ported)

- **URL**: <https://github.com/hortocam/jarvis_ai>
- **License**: MIT
- **Already applied**: Numbered panel sections (01-06), activity feed, cinematic boot, KV rows, status dots

## 4. Community ecosystem

- **Rango** — Browser voice navigation (Vimium for Talon)
- **Cursorless** — Parse-tree voice coding
- **gaze-OCR** — Eye tracking + OCR selection
- **Parrot** — Noise/click control
- **AXKit** — macOS accessibility

## Design Principles Extracted

1. **Always-visible state** — mode, mic, active language shown at a glance
2. **Speech as first-class data** — log commands, show history, make debuggable
3. **Walk-through > reference** — guide users step-by-step, not wall-of-text docs
4. **Context-aware layout** — what you see changes what the HUD shows
5. **WYSIWYS** — if text is visible on screen, saying it activates it
6. **Voice + keyboard + click** — never force one input modality
7. **Ephemeral by default** — command log auto-clears, screen returns to calm
8. **Layered help** — quick help → content toolkit → walkthroughs → docs
