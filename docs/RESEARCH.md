# ai-os — Competitive / Landscape Research

Date: 2026-07-13. Goal: survey existing "AI-OS" / AI-agent-OS / agent-orchestration
TUI projects so we (a) don't reinvent, (b) find our wedge, (c) steal the good ideas.

## TL;DR

The space splits into 4 buckets. Our build overlaps bucket 2/3 but our *unique
combination* is **multimodal input (joystick + voice + kb + trackpad) + multi-harness
abstraction + live stats cockpit**. Nobody ships that as a maintained product.

| Bucket | Examples | What they are | Gap vs ai-os |
|--------|----------|---------------|--------------|
| AI-OS *kernel* | agiresearch/AIOS (6.1k★), OpenDAN (2k★) | OS abstraction layer for agents (LLM/memory/tool managers) | not UX/TUI focused; heavy infra |
| Agent-loop *orchestrator* (TUI) | Ralph TUI (2.4k★) | mission-control for a single coding-agent loop | keyboard-only; one agent type |
| Model-manager TUI | parllama, ollama-tui | single-provider model UIs | narrow; no orchestration |
| Web mission-control | builderz-labs/mission-control, crshdn/mission-control | self-hosted web dashboards | web, not TUI/terminal |

## 1. agiresearch / AIOS — the academic "LLM-as-kernel" reference

- 6.1k★, 803 commits, Python 97%. Paper: "AIOS: LLM Agent Operating System"
  (COLM 2025), preceded by "LLM as OS, Agents as Apps" (arxiv 2312.03815).
- Concept: **AIOS kernel = abstraction layer over the OS kernel**, managing
  LLM, memory, storage, tool resources. SDK (Cerebrum) lets devs build agents.
  Supports Web UI + Terminal UI.
- Deployment "machines": AHM (hub), AUM (UI/client), ADM (dev), ARM (run).
  Modes: Local Kernel, Remote Kernel (run agents on a beefy box, view from a
  thin client). **This directly validates our "remote harness" design.**
- Takeaways for us:
  - The "manager modules" mental model (LLM Core / Context / Memory / Tool)
    maps cleanly onto our Harness + Stats. We don't need a real kernel, but
    the *resource abstraction* framing is right.
  - Remote-kernel mode = a harness that talks to a remote runtime. Already in
    our architecture; make it a first-class harness type.
  - Don't try to be a kernel. Be the *cockpit* on top.

## 2. Ralph TUI — the closest real competitor

- subsy/ralph-tui, 2.4k★, TypeScript/Bun, 1.4k commits. "AI Agent Loop
  Orchestrator": selects highest-priority task → builds prompt → runs your
  agent (Claude Code / OpenCode / Factory Droid) → detects completion → repeats.
- Features we should mirror:
  - **Crash-safe state:** writes `.ralph-tui/session.json`; survives crash,
    resumes. → we currently keep TaskRegistry in memory only. ADD JSON
    persistence + resume.
  - **Per-task control:** pause / resume / kill a single task without nuking
    the session. → we have cancel only. ADD pause/resume.
  - **Model-agnostic / tiered strategy:** cheap model (Haiku) for grunt work,
    Opus for architecture. → we have `active_harness` switch; add a "tier"
    concept per harness.
  - **Safe-run / iteration limit:** `--iterations 5` to cage the loop. → add a
    max-steps guard to harnesses.
- Where it stops: keyboard only, one loop, coding-agent-centric. No joystick,
  no voice, no multi-harness stats viz, no customizable workspaces.
- **Our positioning vs Ralph:** "Ralph orchestrates one coding-agent loop from
  the keyboard. ai-os is a multimodal, multi-harness cockpit: switch harnesses
  and desktops like flipping channels, watch every task's live progress +
  VRAM/throughput, drive it all by joystick, voice, trackpad, or keys."

## 3. Model-manager TUIs (parllama, ollama-tui) — narrow but mature

- parllama: manage/pull/delete/quantize Ollama models from TUI. ollama-tui:
  Rust+Ratatui live monitor polling Ollama API.
- Takeaway: the *model registry / telemetry* widget we sketched is well-trodden;
  we can source real VRAM from `ollama ps` / `nvidia-smi` / vLLM metrics instead
  of fake numbers. Steal the polling pattern.

## 4. The multimodal-input wedge — validated but NOT productized

- A Reddit r/AI_Agents post ("Gamepad as a Local Coding-Agent Control Surface",
  Stadia controller repurposed) shows real demand for gamepad-as-agent-control.
  It's a one-off experiment, not maintained. **This is our opening.**
- Our Intent-bus architecture (keyboard/trackpad/joystick/voice → same Intents)
  is the differentiator. Ralph + parllama are keyboard-only. We own the
  "control surface" angle.
- Voice: keep offline (faster-whisper) given CGNAT/no-public-IP. AIOS/others are
  cloud-API by default; local-first is a selling point for us.

## 5. awesome-tuis / awesome-ratatui — TUI ecosystem is healthy

- Confirmed: TUI tooling (Textual, Ratatui) is mature; dashboards, model UIs,
  k8s/agent orchestrators all exist. No need to fight the medium.
- Library choice: we picked Textual (Python) — integrates Cyclops (Python) and
  is fastest to iterate. If perf becomes an issue, hot paths port to Rust/Ratatui
  (VT Code proves Rust+Ratatui coding-agent TUI works).

## What we should adopt from the research

1. **Crash-safe persistence** — dump TaskRegistry to `~/.ai-os/session.json`;
   resume on launch. (Ralph lesson.)
2. **Pause / resume / kill** per task, not just cancel. (Ralph lesson.)
3. **Tiered harness strategy** — mark each harness cheap|standard|premium and let
   a "route by tier" command pick. (Ralph lesson.)
4. **Iteration/safe-run guard** on autonomous loops. (Ralph lesson.)
5. **Real model telemetry** — poll Ollama/vLLM/nvidia-smi for VRAM + throughput
   instead of fake stats. (parllama/ollama-tui lesson.)
6. **Remote-harness as first-class type** — run work on ARM, view on AUM.
   (AIOS lesson.)
7. **Lean into multimodal input as the brand** — joystick + voice + trackpad +
   keys through one Intent bus. This is the gap nobody fills.

## What we should NOT do

- Build an actual OS kernel / scheduler. AIOS owns that; we're the cockpit.
- Chase web dashboards. The terminal + TUI is our lane and our edge.
- Support only one agent type. Multi-harness is the point.

## Positioning statement (draft)

> ai-os is a customizable, multimodal AI cockpit. It abstracts every AI backend
> (local LLMs, OpenCode, Claude Code, your Cyclops agent, shell, remote runtimes)
> behind one swappable harness interface, shows live per-task progress and
> per-harness stats in a spatial "desktop" layout, and lets you drive the whole
> thing with keyboard, trackpad, a gamepad/joystick, or your voice — all through
> a single intent bus. It's the mission-control layer for your AI fleet, built
> local-first for the terminal.
