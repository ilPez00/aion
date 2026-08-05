"""A branch that indexes past its own split can never run.

`_run_command` opened with `parts = text.split(" ", 1)` and two branches
further down tested `len(parts) >= 3` and `len(parts) >= 4`, reading `parts[2]`
and `parts[3]`. Both were dead — `run <harness> <prompt>` fell through to a
fallback that used the wrong harness, and `setup set KEY VAL` never wrote
anything. Neither is visible from the branch itself: the split is a hundred
lines earlier, and everything in between reads like ordinary argument
handling.

Nothing catches this. Not the type checker (the code is well-typed), not the
tests (a dead branch has no behaviour to assert on), not review (you would have
to be holding the split in your head). It is a two-line arithmetic fact about
one function, which makes it exactly the kind of thing to check mechanically.

So: for every function, find the locals assigned from a split with a maxsplit,
then fail on any `len(v) >= k` or `v[i]` in that same function that the split
makes impossible. Function-scoped on purpose — `_agent_command` splits without
a limit two methods away, and a file-wide check calls its `parts[3]` a bug.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SOURCES = sorted(
    [p for p in (ROOT / "src").rglob("*.py")]
    + [p for p in (ROOT / "scripts").rglob("*.py")]
)


def _limit_of(call: ast.Call) -> int | None:
    """The most parts a `.split(...)` call can produce, or None if unbounded."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "split"):
        return None
    if len(call.args) == 2 and isinstance(call.args[1], ast.Constant):
        if isinstance(call.args[1].value, int):
            return call.args[1].value + 1
    for kw in call.keywords:
        if kw.arg == "maxsplit" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, int):
                return kw.value.value + 1
    return None


def unreachable_indexing(tree: ast.AST) -> list[tuple[int, str]]:
    """Every access a function's own split makes impossible."""
    found: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # name -> [(lineno, cap)], because one name is routinely rebound by
        # several splits in one dispatcher. Taking the narrowest of them is
        # wrong: those bindings live in mutually exclusive branches that each
        # return, so a `sub` split three ways here says nothing about a `sub`
        # split two ways in the branch below. The binding that governs a use
        # is the nearest one ABOVE it.
        binds: dict[str, list[tuple[int, int]]] = {}
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                cap = _limit_of(node.value)
                if cap is not None:
                    binds.setdefault(node.targets[0].id, []).append(
                        (node.lineno, cap))
        if not binds:
            continue

        # `binds` bound as a default: it is rebuilt every iteration of the
        # outer loop, and a closure over the loop variable would read whichever
        # function was analysed last.
        def cap_at(name: str, line: int, binds=binds) -> int | None:
            above = [(ln, c) for ln, c in binds.get(name, ()) if ln < line]
            return max(above)[1] if above else None
        for node in ast.walk(fn):
            if (isinstance(node, ast.Compare)
                    and isinstance(node.left, ast.Call)
                    and isinstance(node.left.func, ast.Name)
                    and node.left.func.id == "len"
                    and node.left.args
                    and isinstance(node.left.args[0], ast.Name)
                    and len(node.comparators) == 1
                    and isinstance(node.comparators[0], ast.Constant)
                    and isinstance(node.comparators[0].value, int)):
                name = node.left.args[0].id
                cap = cap_at(name, node.lineno)
                k, op = node.comparators[0].value, node.ops[0]
                if cap is not None and (
                        (isinstance(op, (ast.GtE, ast.Eq)) and k > cap)
                        or (isinstance(op, ast.Gt) and k >= cap)):
                    found.append((node.lineno,
                                  f"len({name}) tested against {k}, "
                                  f"but the split caps it at {cap}"))
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, int)):
                name = node.value.id
                cap = cap_at(name, node.lineno)
                if cap is not None and node.slice.value >= cap:
                    found.append((node.lineno,
                                  f"{name}[{node.slice.value}] but the split "
                                  f"caps {name} at {cap}"))
    return sorted(set(found))


# ── the checker itself ──────────────────────────────────────────────────────

def test_it_finds_the_bug_that_shipped():
    """The `run <harness> <prompt>` branch, as it actually was."""
    src = '''
def handle(text):
    parts = text.split(" ", 1)
    if parts[0] == "run" and len(parts) >= 3:
        return parts[1], parts[2]
'''
    hits = unreachable_indexing(ast.parse(src))
    assert len(hits) == 2
    assert "len(parts) tested against 3" in hits[0][1]
    assert "parts[2]" in hits[1][1]


def test_it_leaves_an_unlimited_split_alone():
    """`_agent_command` splits without a limit two methods away. A file-wide
    check calls its `parts[3]` a bug; this one must not."""
    src = '''
def handle(text):
    parts = text.split()
    return parts[3] if len(parts) >= 4 else ""
'''
    assert unreachable_indexing(ast.parse(src)) == []


def test_it_scopes_per_function():
    src = '''
def a(text):
    parts = text.split(" ", 1)
    return parts[1]

def b(text):
    parts = text.split()
    return parts[5]
'''
    assert unreachable_indexing(ast.parse(src)) == []


def test_the_nearest_binding_above_a_use_is_the_one_that_governs():
    src = '''
def handle(text):
    parts = text.split(" ", 3)
    parts = text.split(" ", 1)
    return parts[2]
'''
    assert unreachable_indexing(ast.parse(src))


def test_two_branches_reusing_one_name_are_not_merged():
    """The false positive this checker produced on its first run. `sub` is
    split three ways in one branch and two in another; both branches return,
    so neither constrains the other. Taking the narrowest would call working
    code dead — which is a worse failure than the bug being hunted."""
    src = '''
def handle(text, parts):
    if parts[0] == "a":
        sub = parts[1].split(" ", 2)
        if len(sub) == 3:
            return sub[2]
    if parts[0] == "b":
        sub = parts[1].split(" ", 1)
        return sub[1]
'''
    assert unreachable_indexing(ast.parse(src)) == []


def test_maxsplit_by_keyword_counts_too():
    src = '''
def handle(text):
    parts = text.split(maxsplit=2)
    return parts[3]
'''
    assert unreachable_indexing(ast.parse(src))


# ── the tree ────────────────────────────────────────────────────────────────

def test_no_branch_indexes_past_its_own_split():
    problems = []
    for path in SOURCES:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for line, why in unreachable_indexing(tree):
            problems.append(f"{path.relative_to(ROOT)}:{line}  {why}")
    assert not problems, "unreachable argument handling:\n" + "\n".join(problems)
