# physis_pro integration — how the coherence brain plugs into aion's loops

`physis.py` is a thin, dependency-free (`urllib`) client for Gio's Rust
`physis_pro` engine (`http://127.0.0.1:19876`, env `PHYSIS_URL`/`PHYSIS_PORT`).
It **soft-fails everywhere**: if physis is down, callers get neutral results and
a log line, never an exception that stalls the cockpit.

## What physis offers aion

| Endpoint / client call        | Meaning for aion |
|-------------------------------|------------------|
| `classify(text)` → cells      | What **domain of work** a task/output is (semiotic grid, e.g. `CONSTRUCT/REST`) + a match score. |
| `register(node, score, edge)` | Persist a **coherence fact**: +1 flowing / 0 idle / −1 blocked, optionally edged to a domain. |
| `ingest(node, edges)`         | Add a node + edges to the **shared holarchy vector graph** (knowledge that survives across runs). |
| `reconstruct()`               | Browse the accumulated wiki by content. |

## Where it was already wired

- `store.py` (`_spawn`): on task creation, `classify(prompt)` tags the task with
  a `domain`, publishes it on the `physis` bus topic (Tasks workspace shows it),
  and `register`s the new task at coherence +1.
- `harnesses.PhysisHarness`: background poller of embedder health for the
  `physis` workspace HUD.

## What this change adds (loop-level integration)

The two long-running loops now **close the coherence feedback loop** — they
don't just get classified at birth, they report their *outcome* back to the brain.

### Factory loop (`factory.py` + `FactoryHarness`)
- **Stall detection is pure and physis-independent.** `output_novelty()` (difflib
  trailing-window diff) + `detect_stall()` catch a spinning Ralph loop — one whose
  output stopped changing — and stop it with `STOP_STALLED` instead of burning the
  whole budget. This never depends on physis being up. Off by default in the
  engine (`stall_window=0`); the harness defaults it on (`stall_window=3`).
- **physis scores each round (opt-in).** With `extra.coherence=true`, each
  iteration's output is scored via `physis.score_text()` (top-cell score → [−1,1])
  and stored on `Iteration.coherence`.
- **Those scores can end a run (opt-in, off by default).** `coherence_window: N`
  stops the loop with `STOP_INCOHERENT` once N consecutive *readings* sit at or
  below `coherence_floor` (default −0.2). This is the failure novelty cannot
  see: a loop repeating itself is stalled, but a loop producing **fresh output
  about the wrong thing** looks maximally novel right up to the budget. The
  guard is deliberately hard to arm and easy to trust:
  - `coherence_window=0` is the default in the engine *and* the harness. An
    upgrade must not start ending runs that used to finish.
  - A window set without `coherence: true` is refused and logged, not silently
    kept — a guard that can never fire must not read as if it will.
  - **A score of exactly 0.0 is "no reading", not "bad".** `score_text` returns
    0.0 when physis is down, when classify degrades and when the output is
    empty; counting those would make an unreachable brain kill every loop,
    inverting the soft-fail rule below. The most recent round must carry a real
    reading before the guard decides at all.
  - When stall and drift fire on the same round the loop reports `stalled`:
    novelty is measured on the text in hand, coherence is a remote model's
    opinion, and the certain signal is the better thing to have in the log.
  - It only *ends* work (INTERRUPTED, like a budget stop). It never fails a
    task and never starts one, so the worst a wrong score can do is stop early.
- **Outcome recorded.** When the loop ends, `record_outcome(task, ±1/0, domain)`
  tells physis whether that task-domain **flowed** (DONE → +1) or **blocked**
  (ERROR/STALLED/INCOHERENT → −1). Over time physis's dream loop can see which kinds of work
  keep stalling.

### Research loop (`ResearchHarness`)
- On a completed run, `_ingest_research()` pushes the run into the holarchy:
  `ingest("research:<id>", [source urls])` — so discovered knowledge accumulates
  into the shared vector graph and is `reconstruct()`-able later — plus
  `record_outcome(+1 if covered else 0)`.

## Design rules honoured
- **Soft-fail:** every physis call is wrapped; the loop runs identically with
  physis down.
- **Thread-safety:** `score_text` runs inside `run_factory`'s `to_thread` worker
  (blocking urllib, no registry/asyncio access — allowed). Outcome/ingest calls
  run via `asyncio.to_thread` off the event loop so blocking HTTP never stalls
  the UI.
- **Brain scores, engine decides:** physis proposes; the engine owns the stop.
  Stall detection stays pure and local, and coherence gating is opt-in, cannot
  fire on a missing brain, loses ties to the local signal, and can only end a
  run — never fail one, never start one. That is the whole authority a remote
  score is given.

## Next
- Feed `Iteration.coherence` into the DAG edge animation (spec's "kinetic token
  streams") on the HUD.
- Use `reconstruct()` to prime a new research run with what physis already knows
  (skip queries already covered by a past run).
- Pendant vision/audio (`deck/pendant.py`) → `ingest` scene/voice context.
