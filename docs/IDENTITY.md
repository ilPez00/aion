# aion — product identity

**aion is a HUD and application desktop.** It is not a conversational assistant.

## What aion is

| Mode | Meaning |
|------|---------|
| **Splitscreen HUD** | Lives beside your editor / browser / game. Dense status, tasks, system, deck input. Glanceable, not chatty. Low cognitive load when half the screen is code. |
| **Desktop shell** | Launches, tracks, pauses, cancels, and focuses **applications and harnesses**. The unit of work is a *process / task / app*, not a *reply*. |

CyclUno is the physical console for that desktop: navigate workspaces, spawn apps, APP-mode gamepad into the focused program.

## What aion is not

- Not Jarvis-as-personality (no “Done, sir.” product requirement)
- Not a chatbot primary UI (palette + intents yes; freeform chat is secondary tooling)
- Not a single coding-agent loop (that’s Ralph / OpenCode / Hermes — aion *hosts* them as harnesses)
- Not a wearable brain (that’s Cyclops)

## Design principles

1. **Surface over dialogue.** Prefer panels, gauges, task rows, app lists. Speech/TTS is optional telemetry, never the main product.
2. **Intents over chat.** Keyboard, deck, joystick, voice all emit the same `Intent`. The store routes; the UI renders.
3. **Harnesses are backends; apps are first-class.** `AppHarness` (spawn + SIGSTOP/SIGCONT/SIGTERM), Term, remote kernels — manage real programs.
4. **Splitscreen-first density.** Fixed rails, short lines, no multi-paragraph assistant monologues in the center pane.
5. **Degrade gracefully.** No deck / no mic / no LLM → HUD still shows system + tasks + can launch apps.
6. **Wire protocol stays shared.** Deck serial uses cyclops v2 frames; do not invent a parallel framing for “chat personality.”

## Feature gate (before shipping)

Ask for every change:

1. Does this make the **HUD clearer in half a screen**, or the **desktop better at managing apps**?
2. Does it add **assistant theater** (persona wit, unsolicited chat) without improving status/control?

If (2) without (1) → reject or demote to optional theme/voice plugin.

## Related skills (Hermes)

| Skill | Role |
|-------|------|
| `aion-factory-loop` | How to ship cycles in this repo (PDCA + aion gates) |
| `aion-deepsearch` | How to research patterns for HUD / desktop shells |
| `aion-build` | Concrete code map + pitfalls when editing aion |
| `factory-loop` | Generic autonomous production SOP (compose, don’t replace) |
| `deepsearch` | Generic multi-source research SOP (compose, don’t replace) |
