"""swarmpolicy.py — what a DAG does when a step fails.

Until now the answer was: nothing. `SwarmRunner.fail` set the agent FAILED,
`dep_state` turned every dependent into "blocked", and the DAG stopped with a
sentence in a log. That is correct behaviour for a step that is genuinely
broken and completely wrong for the failure a swarm actually hits most often —
a dropped tunnel, a rate limit, a CLI that exited 1 because a socket blinked.
Those are the ones a human retries by hand thirty seconds later, having watched
an unattended DAG sit dead all night for a reason that had already gone away.

THREE QUESTIONS, KEPT APART
---------------------------
    classify()      is this failure the kind that could go away on its own?
    should_retry()  given that, and how many attempts are gone, try again?
    backoff()       and how long should we wait first?

Keeping them separate is not ceremony: the first is a guess about a string, the
second is policy, the third is arithmetic. Only the first can be wrong in an
interesting way, and it is the only one worth arguing about in a test.

WHY THE DEFAULT IS NO RETRY
---------------------------
`RetryPolicy()` with no arguments retries nothing, so behaviour is unchanged
until somebody configures it. A scheduler that starts silently re-running work
after an upgrade is a scheduler that spends money nobody agreed to — and the
budget governor bounds retries but does not consent to them.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
No jitter. Retry storms matter when a thousand clients back off in lockstep
against one service; this is a single cockpit running at most a handful of
steps, and a deterministic delay is worth far more in a test than the collision
it avoids in production.
"""
from __future__ import annotations

from dataclasses import dataclass

# Substrings of an error message that mean "the world was briefly wrong".
# Matching on text is crude, and it is what there is: aion drives external
# CLIs, which report an exit code and whatever they printed. A structured error
# taxonomy would need every harness to agree on one, and none of them do.
TRANSIENT_MARKERS = (
    "timeout", "timed out", "deadline",
    "connection", "connect", "network", "unreachable", "dns",
    "reset by peer", "broken pipe", "eof",
    "temporarily", "try again", "overloaded", "capacity",
    "rate limit", "ratelimit", "429",
    "500", "502", "503", "504", "bad gateway", "gateway timeout",
    "stopped answering",          # our own remote-watch loss message
    "did not accept the task",    # a peer that was busy, not a bad request
)

# ...and the ones that mean "asking again will get the same answer". These win
# over a transient match: re-running a request that was refused for what it IS
# costs money and changes nothing, so an ambiguous message is treated as the
# expensive-to-be-wrong-about case.
PERMANENT_MARKERS = (
    "401", "403", "404", "unauthorized", "forbidden", "not found",
    "no such", "does not exist", "invalid", "malformed", "unsupported",
    "api key", "authentication", "permission denied",
    "context length", "too long", "maximum context",
    "no way to reach",            # no transport configured for that instance
    "refused to", "cannot comply", "policy",
    "syntax error", "traceback",
)


def classify(error: str) -> str:
    """"transient", "permanent" or "unknown" for one error message. Pure."""
    text = (error or "").lower()
    if not text.strip():
        return "unknown"
    permanent = any(m in text for m in PERMANENT_MARKERS)
    transient = any(m in text for m in TRANSIENT_MARKERS)
    if permanent:
        return "permanent"
    if transient:
        return "transient"
    return "unknown"


@dataclass(frozen=True)
class RetryPolicy:
    """How hard to try before a step is somebody's problem.

    `max_attempts` counts TOTAL runs, not extra ones: 1 is the historical
    behaviour (one go, then FAILED), 3 means the original plus two retries.
    Counting extras instead is how an off-by-one becomes 50% more spend.
    """
    max_attempts: int = 1
    base_delay: float = 5.0
    factor: float = 2.0
    max_delay: float = 300.0
    # Retry a failure we could not classify? True is right for CLI harnesses,
    # where most messages match nothing at all and most of those are flaky
    # process problems.
    retry_unknown: bool = True
    # Retry one we classified as permanent? Off, and it should stay off unless
    # a specific harness proves the classifier wrong about it.
    retry_permanent: bool = False

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 1


def backoff(attempts: int, policy: RetryPolicy) -> float:
    """Seconds to wait before attempt number `attempts + 1`. Pure.

    Exponential from the first retry: attempts=1 gives base, 2 gives base *
    factor, and so on, capped at `max_delay` so a long-running DAG cannot back
    itself off into next week.
    """
    if attempts < 1:
        return 0.0
    delay = policy.base_delay * (policy.factor ** (attempts - 1))
    return float(min(max(0.0, delay), max(0.0, policy.max_delay)))


def should_retry(error: str, attempts: int,
                 policy: RetryPolicy) -> tuple[bool, str]:
    """Try this step again? (decision, reason). Pure.

    `attempts` is how many runs are already spent, including the one that just
    failed. The reason is filled in either way, because "why did this stop
    retrying" is the question asked at 3am and the answer must not have to be
    reconstructed from a policy object and a count.
    """
    kind = classify(error)
    if not policy.enabled:
        return False, "retries are off"
    if attempts >= policy.max_attempts:
        return False, (f"{attempts} of {policy.max_attempts} attempts used")
    if kind == "permanent" and not policy.retry_permanent:
        return False, "the error will not fix itself"
    if kind == "unknown" and not policy.retry_unknown:
        return False, "the error could not be classified"
    wait = backoff(attempts, policy)
    return True, (f"{kind} failure, attempt {attempts + 1} of "
                  f"{policy.max_attempts} in {wait:.0f}s")


def policy_from_config(cfg) -> RetryPolicy:
    """Build a policy out of the cockpit config. Never raises.

        {"swarm_retry": {"max_attempts": 3, "base_delay": 5}}

    A shorthand integer is accepted too, because `"swarm_retry": 3` is what
    somebody will write first:

        {"swarm_retry": 3}

    A typo yields the default rather than a cockpit that will not boot, in
    exactly the same way a mistyped price does.
    """
    raw = None
    try:
        raw = (cfg or {}).get("swarm_retry")
    except AttributeError:
        return RetryPolicy()
    if raw is None:
        return RetryPolicy()
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return RetryPolicy(max_attempts=max(1, int(raw)))
        except (TypeError, ValueError):
            return RetryPolicy()
    if not isinstance(raw, dict):
        return RetryPolicy()
    d = RetryPolicy()
    try:
        return RetryPolicy(
            max_attempts=max(1, int(raw.get("max_attempts", d.max_attempts))),
            base_delay=float(raw.get("base_delay", d.base_delay)),
            factor=float(raw.get("factor", d.factor)),
            max_delay=float(raw.get("max_delay", d.max_delay)),
            retry_unknown=bool(raw.get("retry_unknown", d.retry_unknown)),
            retry_permanent=bool(raw.get("retry_permanent", d.retry_permanent)),
        )
    except (TypeError, ValueError):
        return RetryPolicy()
