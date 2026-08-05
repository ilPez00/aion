# aion — product identity

**aion is a HUD and a control surface for work that runs without you.** It is
not a conversational assistant.

The original line here was "a HUD and application desktop", and the unit of
work was "a process / task / app, not a reply". That was true and is now
incomplete. aion plans multi-step work as a DAG, runs the steps across
harnesses and machines, prices them against a budget, holds the dangerous ones
for a human, grows the plan from its own results, and reaps the steps that stop
answering. The unit of work is still not a reply — but it is no longer just a
process either. It is **a step in a plan**, and most of them run while nobody
is looking at the screen.

That is the thing the product is actually for, and every other claim below
follows from it.

## What aion is

| Mode | Meaning |
|------|---------|
| **Splitscreen HUD** | Lives beside your editor / browser / game. Dense status, tasks, system, deck input. Glanceable, not chatty. Low cognitive load when half the screen is code. |
| **Desktop shell** | Launches, tracks, pauses, cancels and focuses applications and harnesses. Real processes, real signals. |
| **Orchestrator** | Plans a goal into a DAG, schedules it under limits it can state (slots, VRAM, money), runs steps locally or on a peer, retries what is transient, and stops for a human where it should. |

CyclUno is the physical console for the desktop half: navigate workspaces,
spawn apps, APP-mode gamepad into the focused program.

## What aion is not

- Not Jarvis-as-personality (no "Done, sir." product requirement)
- Not a chatbot primary UI (palette + intents yes; freeform chat is secondary tooling)
- Not a single coding-agent loop — that is Ralph / OpenCode / Hermes, and aion
  *hosts* them as harnesses. What aion adds is the layer above one loop:
  ordering, limits, evidence, and a place to intervene.
- Not a wearable brain (that is Cyclops)
- Not an autonomous agent that decides its own scope. Every bound it can
  exceed is configured off by default and refuses out loud when it binds.

## Design principles

1. **Surface over dialogue.** Panels, gauges, task rows, step waves. Speech and
   TTS are optional telemetry, never the main product.
2. **Intents over chat.** Keyboard, deck, joystick, voice all emit the same
   `Intent`. The store routes; the UI renders.
3. **Harnesses are backends; apps are first-class.** `AppHarness` (spawn +
   SIGSTOP/SIGCONT/SIGTERM), Term, remote kernels — real programs, managed.
4. **Splitscreen-first density.** Fixed rails, short lines, no multi-paragraph
   assistant monologues in the centre pane.
5. **Degrade gracefully.** No deck / no mic / no LLM → the HUD still shows
   system and tasks and can still launch apps.
6. **Wire protocol stays shared.** Deck serial uses cyclops v2 frames. Do not
   invent parallel framing for "chat personality".

### Principles the orchestration work added

7. **Say why, not just what.** "Nothing is running" has about six causes and
   six different next moves. Every stopped state computes its own sentence —
   blocked on which step, retrying in how long, refused by which limit — and
   both surfaces print that sentence verbatim rather than each phrasing it
   their own way. Two renderers disagreeing about one swarm is how a cockpit
   starts getting argued with instead of believed.
8. **New power ships off.** Retry, replanning, coherence stops and the
   heartbeat reaper all default to doing nothing, and unparseable config falls
   back to off rather than to a guess. Upgrading aion must never change what a
   run does. The bounds that matter are checkpointed, because a restart that
   resets a counter turns a bounded policy into an unbounded one.
9. **Absence of a signal is not a signal.** A physis coherence of 0.0 means
   "no reading", not "incoherent". A step that never reported is not a step
   that went quiet. Watchdogs that confuse the two invent the failures they
   were installed to catch.
10. **Evidence is append-only, and never a control channel.** Approvals and
    step transitions append to JSONL beside the snapshot. A record that gets
    rewritten is evidence of nothing, a line torn by a crash costs exactly that
    line, and nothing reads either file back into a running swarm.
11. **A human gate is a gate.** Approval is recorded *before* the waiting work
    is released, decisions carry who made them (`cockpit`, `remote`, `policy`,
    `timeout`), and a broken sink never deadlocks a gate — a cockpit stuck on a
    full disk is worse than a gap in a log, and the gap is printed.
12. **Pure engine, thin harness.** Scheduling, validation, layering and
    liveness are decided in modules with no event loop, no network and no
    clock. Anything that needs a clock takes it as an argument. This is why the
    interesting failures are reproducible in a unit test instead of at 3am.

## Workspaces

Config-driven (`workspaces` in the config), not a fixed set. The shipped
default is three panels; a full install runs ten:

Desktop (⬡) · Models (◈) · Tasks (▤) · Runs · Agent (✦) · Vault (📓) ·
System (🖥) · Term (▣) · Settings (⚙️) · Net

## Feature gate (before shipping)

Ask for every change:

1. Does it make the **HUD clearer in half a screen**, make the **desktop better
   at managing apps**, or make **work that runs unattended easier to trust**?
2. Does it add **assistant theater** (persona wit, unsolicited chat) without
   improving status or control?
3. If it can end, spend, or start work on its own: **is it off by default, does
   it state its bound, and does it say so out loud when it binds?**

(2) without (1) → reject, or demote to an optional theme/voice plugin.
A "yes" to the first half of (3) with a "no" to any of the rest → not ready.

## Related skills (Hermes)

| Skill | Role |
|-------|------|
| `aion-factory-loop` | How to ship cycles in this repo (PDCA + aion gates) |
| `aion-deepsearch` | How to research patterns for HUD / desktop shells |
| `aion-build` | Concrete code map + pitfalls when editing aion |
| `factory-loop` | Generic autonomous production SOP (compose, don't replace) |
| `deepsearch` | Generic multi-source research SOP (compose, don't replace) |
