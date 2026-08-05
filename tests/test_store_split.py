"""The seam between `store.py` and its swarm half.

`store.py` reached 2093 lines. The swarm was the largest single thing in it —
the lazy runner, the remote hooks, the typed verbs, plan/apply, the replan tick
— about 560 lines answering one question, in a file that also handles todos,
env vars, chat, boards and the task registry.

The move was a relocation, not a redesign: a mixin, same `self`, no call site
changed. These tests are about the two ways that quietly comes undone — the
mixin drifting back into `store.py`, and `Store` losing a method the move was
supposed to preserve.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.store import Store  # noqa: E402
from aion.storeswarm import SwarmCommands  # noqa: E402

STORE = ROOT / "src" / "aion" / "store.py"
SWARM = ROOT / "src" / "aion" / "storeswarm.py"

# Every swarm entry point a caller outside this module uses. Losing one is not
# a refactor, it is a missing feature — `swarm_command` alone is reached by the
# TUI, the web transport and the agent tool.
PUBLIC = ("swarm_runner", "swarm_command", "swarm_replan_tick", "task_state")


def test_the_store_still_answers_every_swarm_call():
    for name in PUBLIC:
        assert hasattr(Store, name), name


def test_the_swarm_methods_live_in_the_swarm_module():
    """The point of the move. If these come back, the file goes back to 2100
    lines and the next reader is looking for `swarm plan` in among the todo
    handlers again."""
    src = STORE.read_text(encoding="utf-8")
    for name in ("swarm_runner", "swarm_command", "_swarm_plan", "_swarm_apply",
                 "_swarm_command", "swarm_replan_tick", "_swarm_delegate"):
        assert f"def {name}(" not in src, f"{name} moved back into store.py"


def test_the_mixin_is_actually_mixed_in():
    assert issubclass(Store, SwarmCommands)


def test_store_is_smaller_than_it_was():
    """Not a style rule — a ratchet. 2093 was the number that made the split
    worth doing; anything at or above it means the extraction was undone or
    silently refilled."""
    assert len(STORE.read_text(encoding="utf-8").splitlines()) < 1800


def test_the_swarm_module_does_not_import_the_store():
    """A mixin importing the class it is mixed into is a circular import
    waiting for the first person who reorders the imports."""
    src = SWARM.read_text(encoding="utf-8")
    assert not re.search(r"^from \.store import|^from aion\.store import",
                         src, re.M)


def test_the_mixin_says_what_it_needs_from_the_store():
    """It is coupled — every method reads `self.swarm`, `self.state`,
    `self.registry`. Pretending otherwise is how the next person assumes it
    stands alone. The docstring is the contract."""
    doc = SwarmCommands.__module__ and SWARM.read_text(encoding="utf-8")
    for attr in ("self.swarm", "self.state", "self.registry", "self.harnesses",
                 "self._spawn_now"):
        assert attr in doc.split('"""')[1], f"{attr} undocumented"
