"""A loop's opinion of itself, where someone can see it.

The factory harness writes `task.coherence` every iteration and has done since
coherence scoring existed. `Task.as_dict()` never carried it, so it died in the
process that computed it: every remote view — the web HUD, a peer polling
`/task`, the process graph — had a progress bar and nothing at all about
whether the work was going anywhere. A loop can be 80% through its budget and
producing fluent nonsense, and that looked identical to one about to succeed.

The load-bearing rule is the same one `swarmlive` and the factory's own drift
detector follow: **0.0 means "no reading", not "bad"**. A disabled brain, an
unreachable one, and a harness that does not score at all all report 0.0, and a
viewer that cannot tell those from a measured 0.0 paints an unscored loop as
maximally incoherent — a verdict invented out of an absence. So the wire format
carries `null`, and the renderer leaves a null edge looking exactly as it did.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.core import Task, TaskState  # noqa: E402

HUD_JS = (ROOT / "scripts" / "static" / "hud.js").read_text(encoding="utf-8")
ORGANIC_JS = (ROOT / "scripts" / "static" / "organic.js").read_text(encoding="utf-8")


def task(**kw) -> Task:
    base = dict(id="t1", label="loop", harness="factory",
                state=TaskState.RUNNING)
    base.update(kw)
    return Task(**base)


# ── the wire format ─────────────────────────────────────────────────────────

def test_a_scored_loop_reports_its_score():
    assert task(coherence=0.62).as_dict()["coherence"] == 0.62


def test_a_negative_score_survives_intact():
    """Drift is the reading that matters most; a clamp to zero here would
    throw away the only value anyone would act on."""
    assert task(coherence=-0.45).as_dict()["coherence"] == -0.45


def test_no_reading_is_null_rather_than_zero():
    """The whole discipline in one assertion. 0.0 is what a dead brain
    returns, so it must not be transmitted as a measurement."""
    assert task().as_dict()["coherence"] is None


def test_a_finished_task_reports_no_novelty():
    """Novelty describes the step just taken. On a task that has stopped it is
    a stale number that reads as live."""
    assert task(state=TaskState.DONE, novelty=0.9).as_dict()["novelty"] is None


def test_a_running_task_reports_novelty():
    assert task(novelty=0.4).as_dict()["novelty"] == 0.4


def test_the_rest_of_the_payload_is_unchanged():
    """This shape is read by the HUD, the process graph and peers. Adding to
    it is fine; moving anything is not."""
    d = task(progress=0.5).as_dict()
    for key in ("id", "label", "harness", "domain", "state", "progress",
                "eta", "log"):
        assert key in d


# ── the browser ─────────────────────────────────────────────────────────────

def test_the_hud_passes_coherence_onto_the_edge():
    assert "coherence: coh" in HUD_JS


def test_the_hud_does_not_turn_a_missing_reading_into_zero():
    """`t.coherence || 0` would silently convert "nobody measured" into the
    worst possible reading — the exact bug this format exists to prevent."""
    assert "typeof t.coherence === 'number' ? t.coherence : null" in HUD_JS
    assert "t.coherence || 0" not in HUD_JS
    assert "t.coherence ?? 0" not in HUD_JS


def test_the_renderer_keeps_null_distinct_from_a_number():
    assert "l.coh !== null" in ORGANIC_JS
    assert "l.coherence ?? 0" not in ORGANIC_JS


def test_an_unscored_edge_is_drawn_exactly_as_before():
    """Everything coherence does to an edge is inside the `!== null` guard, so
    a graph with no scoring anywhere looks untouched."""
    body = re.search(r"let cohSag = 0;(.*?)const mx =", ORGANIC_JS, re.S)
    assert body, "the coherence block moved — check the guard is still there"
    guarded = body.group(1)
    assert "l.coh !== null" in guarded
    # No assignment to the shared drawing state outside the guard.
    before = guarded.split("if (l.coh !== null")[0]
    assert "ctx." not in before


def test_the_animation_respects_reduced_motion():
    """A pulsing line is motion, and the HUD honours the OS setting elsewhere;
    an animation that ignores it is an accessibility regression."""
    block = re.search(r"if \(l\.coh !== null.*?\n    \}", ORGANIC_JS, re.S)
    assert block and "REDUCED" in block.group(0)
