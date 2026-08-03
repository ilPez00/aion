"""swarmbudget.py — what a DAG is allowed to spend.

`admit()` already refuses to start more work than the machine can hold:
parallelism and VRAM. Neither of those is money. A swarm left running
unattended — which is the entire point of one — can sit inside both limits and
still spend all night, because "three at a time" says nothing about what three
API-backed steps cost per hour. The stall guard stops a DAG that is stuck; this
stops one that is working perfectly and is simply too expensive.

RESERVE, THEN RECONCILE
-----------------------
The prompt is known before a step runs; the output is not. So admission
reserves an estimate — like a card hold — and the reservation is replaced by
the real figure when the step finishes. Without the hold, N steps admitted in
one tick each see the same "spent so far" and the budget is blown N times over
in a single pump.

WHAT THIS CAN AND CANNOT SEE
----------------------------
aion drives external CLIs. They report stdout and an exit code, not token
counts, so nothing here is a bill — it is characters divided by four and
multiplied by a price somebody configured. That is honest as a GOVERNOR (stop
before roughly £X) and dishonest as ACCOUNTING, so nothing in this module is
called "spend" or "cost so far" without the word estimated nearby. A harness
that reports real usage can call `settle()` with actual token counts and the
estimate is discarded.

A harness with no configured price contributes zero. Local models are free at
the margin, and pretending otherwise would make the budget fire on a swarm
that costs nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Rough across English and code, and it is the ratio every provider's own
# "estimating tokens" page reaches for. Being wrong by 20% does not matter for
# a stop-at-a-limit decision; being wrong by 10x would, and it is not.
CHARS_PER_TOKEN = 4

# What a step is assumed to produce when nobody knows yet. Deliberately not
# small: a reservation that under-estimates lets a DAG past the limit and only
# discovers it afterwards, which is the one failure this module exists to
# prevent.
DEFAULT_OUTPUT_TOKENS = 1500

UNLIMITED = 0.0          # a budget of 0 means no budget, matching vram_total


@dataclass(frozen=True)
class Price:
    """Currency per million tokens. Zero means unmetered, not free-and-unknown."""
    input_per_m: float = 0.0
    output_per_m: float = 0.0

    @property
    def metered(self) -> bool:
        return bool(self.input_per_m or self.output_per_m)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (max(0, input_tokens) * self.input_per_m / 1_000_000
                + max(0, output_tokens) * self.output_per_m / 1_000_000)


def estimate_tokens(text: str) -> int:
    """Characters over four, never less than one for non-empty text. Pure."""
    text = text or ""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_cost(prompt: str, price: Price,
                  output_tokens: int = DEFAULT_OUTPUT_TOKENS) -> float:
    """What a step is expected to cost before it runs. Pure."""
    return price.cost(estimate_tokens(prompt), output_tokens)


def affordable(cost: float, committed: float,
               budget: float = UNLIMITED) -> tuple[bool, str]:
    """May this step be admitted against the budget? Pure.

    A step that cannot fit even an empty budget is called out by name rather
    than deferred, for the same reason `admit()` does it for VRAM: a DAG that
    silently stops is the failure this whole area exists to prevent.
    """
    if budget <= UNLIMITED:
        return True, ""
    if cost > budget:
        return False, (f"one step is estimated at {cost:.2f} but the whole "
                       f"budget is {budget:.2f} — it can never be admitted")
    if committed + cost > budget:
        return False, (f"{committed + cost:.2f} would exceed the {budget:.2f} "
                       f"budget (estimated)")
    return True, ""


@dataclass
class Entry:
    """One step's line in the ledger."""
    agent_id: str
    harness: str
    reserved: float = 0.0
    actual: float | None = None       # None until the step finishes

    @property
    def committed(self) -> float:
        """What this step is holding: the real figure once known, else the hold."""
        return self.reserved if self.actual is None else self.actual


@dataclass
class Ledger:
    """Reservations and settlements for one swarm.

    Not a general accounting object: it exists so `admit()` can ask one
    question — how much of the budget is already spoken for.
    """
    prices: dict[str, Price] = field(default_factory=dict)
    entries: dict[str, Entry] = field(default_factory=dict)
    output_tokens: int = DEFAULT_OUTPUT_TOKENS
    # What earlier attempts by the same agent already came to. Entries are
    # keyed by agent id and a retry reserves against that same id, so without
    # this the second attempt overwrites the first and every retry is free —
    # which is precisely backwards, since retries are the one thing a budget
    # exists to bound.
    sunk: dict[str, float] = field(default_factory=dict)

    def price_for(self, harness: str) -> Price:
        return self.prices.get(harness or "", Price())

    def estimate(self, harness: str, prompt: str) -> float:
        return estimate_cost(prompt, self.price_for(harness), self.output_tokens)

    def reserve(self, agent_id: str, harness: str, prompt: str) -> float:
        """Hold an estimate against the budget. Returns what was held.

        A second reservation for the same agent is a RETRY, so whatever the
        previous attempt settled at is banked first. It is spent either way,
        and the budget must go on seeing it.
        """
        previous = self.entries.get(agent_id)
        if previous is not None and previous.actual is not None:
            self.sunk[agent_id] = self.sunk.get(agent_id, 0.0) + previous.actual
        cost = self.estimate(harness, prompt)
        self.entries[agent_id] = Entry(agent_id, harness or "", reserved=cost)
        return cost

    def settle(self, agent_id: str, prompt: str = "", output: str = "",
               input_tokens: int | None = None,
               output_tokens: int | None = None) -> float:
        """Replace a reservation with what the step really came to.

        `input_tokens`/`output_tokens` are for a harness that reports real
        usage; everything else is measured off the text it produced.
        """
        entry = self.entries.get(agent_id)
        if entry is None:
            return 0.0
        price = self.price_for(entry.harness)
        entry.actual = price.cost(
            estimate_tokens(prompt) if input_tokens is None else input_tokens,
            estimate_tokens(output) if output_tokens is None else output_tokens)
        return entry.actual

    def release(self, agent_id: str) -> None:
        """Drop a reservation entirely — the step never ran.

        Distinct from settling at zero: a cancelled step produced nothing AND
        owes nothing, and leaving a hold behind would shrink the budget
        available to the rest of the DAG for no reason.

        Earlier ATTEMPTS by the same agent are not dropped. Cancelling a step
        on its third try does not refund the two runs that already happened.
        """
        self.entries.pop(agent_id, None)

    def committed(self) -> float:
        """Everything held or spent, estimated. What `affordable` compares to."""
        return (sum(e.committed for e in self.entries.values())
                + sum(self.sunk.values()))

    def settled(self) -> float:
        return (sum(e.actual for e in self.entries.values()
                    if e.actual is not None)
                + sum(self.sunk.values()))

    def outstanding(self) -> float:
        return sum(e.reserved for e in self.entries.values() if e.actual is None)

    def metered(self) -> bool:
        """Is any of this actually priced? A wholly local swarm is not."""
        return any(self.price_for(e.harness).metered for e in self.entries.values())

    def as_dict(self) -> dict:
        return {
            "committed": round(self.committed(), 4),
            "settled": round(self.settled(), 4),
            "outstanding": round(self.outstanding(), 4),
            "retried": round(sum(self.sunk.values()), 4),
            "steps": len(self.entries),
            "estimated": True,        # never let a caller mistake this for a bill
        }


def prices_from_harnesses(harnesses) -> dict[str, Price]:
    """Read per-harness pricing out of the harness table.

    Config lives in the harness's `extra` block, because that is where every
    other per-harness knob lives:

        {"id": "claude", "extra": {"input_per_m": 3.0, "output_per_m": 15.0}}

    Anything missing or unparseable is unmetered rather than a crash: a typo
    in a price must not stop the cockpit booting.
    """
    out: dict[str, Price] = {}
    for hid, h in (harnesses or {}).items():
        extra = getattr(getattr(h, "cfg", None), "extra", None) or {}
        try:
            price = Price(float(extra.get("input_per_m", 0) or 0),
                          float(extra.get("output_per_m", 0) or 0))
        except (TypeError, ValueError):
            price = Price()
        if price.metered:
            out[hid] = price
    return out
