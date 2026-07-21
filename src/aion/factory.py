"""factory.py — the factory loop (Ralph-style agent orchestration).

A factory loop runs one agent over and over until the work is done:

    build   render the command for this iteration (prompt + last output)
    run     execute the agent (a shell command, opencode, claude, droid…)
    detect  is it finished? — a sentinel in the output, or a check command
    repeat  until done, or the iteration budget runs out

The one real judgement call is *how you know it's done*. Two signals, checked
in this order:

  - done_marker: a string the agent prints when finished (e.g. "TASK_COMPLETE").
    Cheap, but the agent has to cooperate.
  - done_command: a shell command that exits 0 when the work is complete (e.g.
    "pytest -q" or "test -f build/out"). Objective, but costs a run per round.

If neither is set the loop runs exactly max_iters times — a fixed-length batch.
The engine is pure and injectable: `run_cmd` and `check_cmd` are passed in, so
the loop is tested with no subprocess and no agent installed.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Protocol

# run_cmd(command) -> (exit_code, output_text)
RunCmd = Callable[[str], "tuple[int, str]"]
# check_cmd(command) -> exit_code   (0 == work is complete)
CheckCmd = Callable[[str], int]

# stop reasons
STOP_DONE = "done"          # a completion signal fired
STOP_BUDGET = "budget"      # ran out of iterations
STOP_ERROR = "error"        # the agent command failed hard
STOP_ABORTED = "aborted"    # killed / paused-then-killed


class StepReporter(Protocol):
    def __call__(self, iteration: int, total: int, exit_code: int,
                 tail: str) -> bool: ...


@dataclass
class FactoryConfig:
    command: str                     # template; {n} {p} {last} substituted
    max_iters: int = 10
    done_marker: str = ""            # output substring meaning "finished"
    done_command: str = ""           # shell check; exit 0 == finished
    stop_on_error: bool = True       # a non-zero agent exit ends the loop
    tail_chars: int = 400            # how much prior output feeds the next round


@dataclass
class Iteration:
    n: int
    command: str
    exit_code: int
    output: str
    done: bool = False


@dataclass
class FactoryResult:
    prompt: str
    iterations: list[Iteration] = field(default_factory=list)
    stopped: str = ""
    count: int = 0

    def as_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "stopped": self.stopped,
            "count": self.count,
            "iterations": [
                {"n": it.n, "exit_code": it.exit_code, "done": it.done,
                 "output_tail": it.output[-200:]}
                for it in self.iterations
            ],
        }


# ── pure steps ────────────────────────────────────────────────────────────────
def render_command(template: str, iteration: int, prompt: str,
                   last_output: str, tail_chars: int = 400) -> str:
    """Fill an iteration's command. {n} iteration, {p} prompt, {last} tail.

    The template is trusted config (it IS a shell command), but {p} and {last}
    are data — the prompt may arrive from voice/remote/palette and the output
    is whatever the agent printed. Both are shlex.quoted so a prompt like
    '; rm -rf ~' becomes a single inert argument instead of injecting a
    command. {n} is an int and needs no quoting.
    """
    tail = last_output[-tail_chars:] if last_output else ""
    return (template
            .replace("{n}", str(iteration))
            .replace("{p}", shlex.quote(prompt))
            .replace("{last}", shlex.quote(tail)))


def detect_done(cfg: FactoryConfig, output: str, exit_code: int,
                check_cmd: CheckCmd | None) -> bool:
    """Has the work finished? marker first (free), then the check command."""
    if cfg.done_marker and cfg.done_marker in output:
        return True
    if cfg.done_command and check_cmd is not None:
        return check_cmd(cfg.done_command) == 0
    return False


def stop_reason(cfg: FactoryConfig, iteration: int, done: bool,
                exit_code: int) -> str | None:
    """Why the loop should end after this iteration, or None to continue."""
    if done:
        return STOP_DONE
    if cfg.stop_on_error and exit_code != 0:
        return STOP_ERROR
    if iteration >= cfg.max_iters:
        return STOP_BUDGET
    return None


# ── the loop ──────────────────────────────────────────────────────────────────
def run_factory(prompt: str, cfg: FactoryConfig, run_cmd: RunCmd,
                check_cmd: CheckCmd | None = None,
                report_step: StepReporter | None = None) -> FactoryResult:
    """Run the agent until a completion signal or the iteration budget.

    max_iters is the hard cap (safe-run guard, lesson #4): even with a broken
    completion check the loop cannot run forever.
    """
    result = FactoryResult(prompt=prompt)
    step = report_step or (lambda *a, **k: True)
    max_iters = max(1, cfg.max_iters)
    last_output = ""

    for n in range(1, max_iters + 1):
        command = render_command(cfg.command, n, prompt, last_output,
                                 cfg.tail_chars)
        if not step(n, max_iters, 0, ""):     # kill check before a costly run
            result.stopped = STOP_ABORTED
            return result

        try:
            exit_code, output = run_cmd(command)
        except Exception as e:  # noqa: BLE001
            result.iterations.append(Iteration(n, command, 1, f"error: {e}"))
            result.count = n
            result.stopped = STOP_ERROR
            return result

        done = detect_done(cfg, output, exit_code, check_cmd)
        result.iterations.append(Iteration(n, command, exit_code, output, done))
        result.count = n
        last_output = output

        if not step(n, max_iters, exit_code, output[-200:]):
            result.stopped = STOP_ABORTED
            return result

        reason = stop_reason(cfg, n, done, exit_code)
        if reason:
            result.stopped = reason
            return result

    result.stopped = STOP_BUDGET
    return result
