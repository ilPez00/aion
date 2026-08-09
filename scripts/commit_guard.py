"""commit_guard.py — what must never reach a commit.

`AGENTS.md` says "Never `git add -A`. Stage explicit paths." A rule written in
a document is enforced by whoever remembers it, which in practice means it is
enforced until the one time it matters. This is the same rule as a program.

A hook cannot see HOW something was staged — `git add -A` and a careful
explicit `git add` produce an identical index. What it can see is the SHAPE
that a blind add-all produces and a deliberate one does not: files outside the
project's known layout, files whose names announce they hold credentials, and
blobs nobody meant to commit. So that is what is checked.

Everything here is pure and takes its input as arguments. The hook does the
git calls; this module decides. That split is why the rules have tests instead
of a comment claiming they work.
"""
from __future__ import annotations

import re

# Top-level entries the project actually consists of. A path outside these is
# not necessarily malicious — it is unreviewed, which is the property that
# matters, because the reason `add -A` is banned is that nobody looked.
ALLOWED_TOP = {
    "AGENTS.md", "README.md", "plan.md", "pyproject.toml", "aion.sh",
    "LICENSE", "NOTICE",
    "twa-manifest.json", ".gitignore", ".githooks", ".github",
    "src", "tests", "docs", "config", "scripts", "static",
}

# Names that announce their contents. Matched on the basename, case-folded.
SECRET_NAMES = (
    re.compile(r"^\.env(\..*)?$"),
    re.compile(r"^.*\.pem$"),
    re.compile(r"^.*\.(key|p12|pfx|keystore|jks)$"),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"^(credentials|secrets|vault|token|tokens)\.(json|ya?ml|txt)$"),
    re.compile(r"^.*\.sqlite3?$"),
    re.compile(r"^\.netrc$"),
)

# Content patterns. Every one requires enough length that a placeholder does
# not trip it: a guard that cries wolf gets disabled within a week, and a
# disabled guard is worth less than no guard because it is still believed in.
SECRET_CONTENT = (
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{40,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
)

# A private key is a header AND a body. Matching the header alone flags every
# doc that mentions the format and every test that asserts on it — which is
# how this guard refused its own test file the first time it ran.
PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
KEY_MATERIAL = re.compile(r"^[A-Za-z0-9+/]{40,}={0,2}$")

# A source file is thousands of bytes. Anything past this is a build artefact,
# a dataset or a binary, and none of those belong in a diff somebody reviews.
MAX_BLOB_BYTES = 1_000_000


def path_problem(path: str) -> str:
    """Why this staged path must not be committed, or "" if it is fine. Pure."""
    # `lstrip("./")` would strip a CHARACTER SET, turning ".github/..." into
    # "github/..." and refusing every workflow file. Prefixes, not characters.
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    if not path:
        return ""
    top = path.split("/", 1)[0]
    if top not in ALLOWED_TOP:
        return (f"{top!r} is not part of the project layout — stage explicit "
                f"paths rather than everything the working tree happens to hold")
    base = path.rsplit("/", 1)[-1].lower()
    for pattern in SECRET_NAMES:
        if pattern.match(base):
            return f"{base!r} is the name of a credentials file"
    return ""


def content_problems(diff: str) -> list[tuple[str, str]]:
    """(rule, matched text) for every secret pattern in ADDED lines. Pure.

    Only added lines: a diff that REMOVES a leaked key is the fix, and blocking
    it would be the exact opposite of the intent.
    """
    found: list[tuple[str, str]] = []
    added = [line[1:] for line in diff.splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    for line in added:
        for rule, pattern in SECRET_CONTENT:
            m = pattern.search(line)
            if m:
                text = m.group(0)
                found.append((rule, text[:12] + "…" if len(text) > 12 else text))
    header = next((l for l in added if PRIVATE_KEY_HEADER.search(l)), "")
    if header and any(KEY_MATERIAL.match(l.strip()) for l in added):
        found.append(("private key block", header.strip()[:12] + "…"))
    return found


def size_problem(path: str, size: int, limit: int = MAX_BLOB_BYTES) -> str:
    if size > limit:
        return f"{path} is {size // 1024}KB — over the {limit // 1024}KB limit"
    return ""


# A logical cycle adds a handful of files. A sweep adds whatever the working
# tree happened to hold — and in this repo the tree holds another agent's
# untracked WIP, which is how an SSH-harvesting LAN daemon reached origin once
# already. The allowlist cannot catch that one: it lived under `src/`. Bulk is
# the only shape left that distinguishes the two.
MAX_NEW_FILES = 12


def bulk_problem(new_paths: list[str], limit: int = MAX_NEW_FILES) -> str:
    """Refuse a commit that adds more new files than anyone reviewed. Pure."""
    if len(new_paths) <= limit:
        return ""
    shown = ", ".join(sorted(new_paths)[:4])
    return (f"{len(new_paths)} new files in one commit ({shown}, …) — that is "
            f"the shape of a sweep, not a cycle; commit them in pieces you "
            f"have read")


def report(paths: list[str], diff: str,
           sizes: dict[str, int] | None = None,
           new_paths: list[str] | None = None) -> list[str]:
    """Every reason this commit should be refused. Empty means allow. Pure."""
    problems = []
    for p in paths:
        why = path_problem(p)
        if why:
            problems.append(f"{p}: {why}")
    for path, size in (sizes or {}).items():
        why = size_problem(path, size)
        if why:
            problems.append(why)
    why = bulk_problem(new_paths or [])
    if why:
        problems.append(why)
    for rule, text in content_problems(diff):
        problems.append(f"looks like a {rule} ({text}) is being added")
    return problems
