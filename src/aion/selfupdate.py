"""selfupdate.py — is this machine running the same aion as the rest?

A fleet drifts. pansa was a commit behind for a day and nothing said so; air
was three weeks behind on a branch nobody remembered checking out. Both were
answering `/status` and accepting work the whole time, and the only way to
find out was to ssh in and run `git rev-parse` by hand on each box.

Two questions, deliberately separate:

    behind origin?   compare local HEAD against `git ls-remote`. This is the
                     one that can be ACTED on -- origin is the source of truth
                     and pulling from it is an ordinary update.
    behind a peer?   compare against the revision each peer reports in its
                     status. This is DIAGNOSIS ONLY.

That second distinction is the whole security posture of this module. Code is
never fetched from a peer. A peer is trusted with fleet membership, not with
supplying the software the fleet runs -- treat one as an update source and any
compromised node owns every other node, which is a worse failure than being a
few commits behind. So a peer's revision is used to say "pansa is ahead of
you", and the fix is always `git pull` from origin.

Checking is off unless asked for, and applying is off separately. An
orchestrator that silently restarts itself onto new code mid-DAG is not a
feature.

Pure: every function takes its facts as arguments or runs one bounded git
command. Nothing here schedules, and nothing writes to a repo unless the
caller explicitly calls `pull`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Revision", "FleetDrift", "UpdatePolicy", "local_revision",
           "upstream_revision", "compare", "describe", "policy_from_config",
           "pull"]

# Bounded, because this runs on a timer inside a cockpit. `ls-remote` talks to
# the network and a hung DNS lookup must not become a frozen UI.
GIT_TIMEOUT = 15


@dataclass(frozen=True)
class Revision:
    """What a machine is running."""

    sha: str = ""            # full or short; compared by prefix
    branch: str = ""
    dirty: bool = False      # uncommitted changes in the working tree

    @property
    def short(self) -> str:
        return self.sha[:7]

    def same_as(self, other: "Revision | str") -> bool:
        """Prefix comparison, because `/status` carries a short sha and
        `ls-remote` returns a full one. Two empty shas are NOT the same: an
        unknown revision matching another unknown is the kind of agreement
        that hides a problem."""
        theirs = other if isinstance(other, str) else other.sha
        if not self.sha or not theirs:
            return False
        n = min(len(self.sha), len(theirs))
        return self.sha[:n] == theirs[:n]


@dataclass
class FleetDrift:
    """Who is running what, and what to do about it."""

    local: Revision = field(default_factory=Revision)
    upstream: str = ""                       # sha at origin, "" if unknown
    peers: dict = field(default_factory=dict)   # peer id -> sha
    behind_origin: bool = False
    # Peers whose revision differs from ours. Not "ahead": without a shared
    # history walk this cannot tell ahead from behind, and claiming to know
    # would be worse than saying they differ.
    differing: list = field(default_factory=list)
    unknown: list = field(default_factory=list)  # peers that reported nothing

    @property
    def in_sync(self) -> bool:
        return not self.behind_origin and not self.differing


def _git(root: Path | str, *args: str) -> str:
    """One bounded git command. Any failure is an empty string.

    A cockpit that cannot start because `git` is missing, or because this is a
    tarball rather than a checkout, would be a worse cockpit. Not knowing the
    revision is a fact to report, not an error to raise.
    """
    try:
        out = subprocess.run(["git", "-C", str(root), *args],
                             capture_output=True, text=True,
                             timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def local_revision(root: Path | str) -> Revision:
    """What this checkout is on. Empty sha when it is not a checkout."""
    sha = _git(root, "rev-parse", "HEAD")
    if not sha:
        return Revision()
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    # `--porcelain` is empty exactly when the tree is clean. Reported because
    # a dirty peer explains a revision that matches origin and behaves
    # differently anyway.
    dirty = bool(_git(root, "status", "--porcelain"))
    return Revision(sha=sha, branch=branch, dirty=dirty)


def upstream_revision(root: Path | str, remote: str = "origin",
                      branch: str = "main") -> str:
    """The sha at origin, without fetching objects.

    `ls-remote` asks and returns; it does not write to the repo. That matters
    for a check on a timer — a background `git fetch` mutating a repo somebody
    is working in is a surprise nobody asked for.
    """
    out = _git(root, "ls-remote", remote, f"refs/heads/{branch}")
    return out.split()[0] if out else ""


def compare(local: Revision, upstream: str = "",
            peers: dict | None = None) -> FleetDrift:
    """Fold the three sources into one verdict. Pure.

    An unknown revision on either side is never treated as agreement: it goes
    in `unknown`, so "I could not tell" reads differently from "they match".
    """
    drift = FleetDrift(local=local, upstream=upstream, peers=dict(peers or {}))
    if upstream and local.sha:
        drift.behind_origin = not local.same_as(upstream)
    for pid, sha in sorted(drift.peers.items()):
        if not sha:
            drift.unknown.append(pid)
        elif not local.same_as(sha):
            drift.differing.append(pid)
    return drift


def describe(drift: FleetDrift) -> str:
    """One line, in the words both surfaces use.

    Says what to DO where there is something to do. "behind origin" has an
    action; "pansa differs" has an investigation, and conflating the two is
    how an operator learns to ignore the line.
    """
    if not drift.local.sha:
        return "not a git checkout — cannot tell what this is running"
    bits = []
    if drift.behind_origin:
        bits.append(f"behind origin ({drift.local.short} → "
                    f"{drift.upstream[:7]}) — git pull")
    if drift.differing:
        names = ", ".join(drift.differing[:3])
        more = f" +{len(drift.differing) - 3}" if len(drift.differing) > 3 else ""
        bits.append(f"differs from {names}{more}")
    if drift.unknown:
        bits.append(f"{len(drift.unknown)} peer(s) did not report a revision")
    if drift.local.dirty:
        # Last, and always said: a dirty tree explains a peer that matches on
        # sha and still behaves differently.
        bits.append("local tree is dirty")
    return " · ".join(bits) if bits else f"up to date ({drift.local.short})"


@dataclass(frozen=True)
class UpdatePolicy:
    """When to look, and whether looking may change anything."""

    # Seconds between checks. 0 = never look.
    check_every: float = 0.0
    # Run `git pull --ff-only` when behind origin WITHOUT asking. Off, and
    # separate from checking: an orchestrator that restarts itself onto new
    # code in the middle of a DAG is not a feature. Fast-forward only, so a
    # local commit is never silently discarded.
    auto_pull: bool = False
    # Ask a human, through the same approval gate everything else dangerous
    # goes through, and apply on yes. This is the middle setting between
    # "tell me" and "just do it", and it is the one most people want: the
    # machine notices, the person decides, and the decision is recorded with
    # who made it. Unanswered is a no — gates fail closed.
    ask: bool = False
    remote: str = "origin"
    branch: str = "main"

    @property
    def enabled(self) -> bool:
        return self.check_every > 0


def policy_from_config(cfg: dict | None) -> UpdatePolicy:
    """Read `updates` off a config. Anything unparseable is OFF.

    `auto_pull` additionally requires checking to be on, so setting one
    without the other cannot produce a policy that pulls without looking.
    """
    raw = (cfg or {}).get("updates")
    if not isinstance(raw, dict):
        return UpdatePolicy()
    try:
        every = max(0.0, float(raw.get("check_every", 0) or 0))
        auto = bool(raw.get("auto_pull", False)) and every > 0
        return UpdatePolicy(
            check_every=every,
            auto_pull=auto,
            # `auto_pull` wins: asking about something already applied without
            # asking is a prompt that means nothing.
            ask=bool(raw.get("ask", False)) and every > 0 and not auto,
            remote=str(raw.get("remote") or "origin"),
            branch=str(raw.get("branch") or "main"))
    except (TypeError, ValueError):
        return UpdatePolicy()


def gate_action(drift: FleetDrift) -> str:
    """The sentence a human is asked to approve.

    Names both revisions and the branch, because "update aion?" is not a
    question anybody can answer — the useful version says what it is moving
    from, to, and on which branch, so an unexpected branch is visible before
    the yes rather than after it.
    """
    return (f"update aion {drift.local.short} → {drift.upstream[:7]} "
            f"on {drift.local.branch or '?'} (git pull --ff-only)")


def can_apply(drift: FleetDrift) -> tuple[bool, str]:
    """Is this machine in a state where applying an update is safe?

    Checked BEFORE asking, so a human is never asked to approve something
    that will then refuse itself — an approval that does nothing is worse
    than no prompt, because it teaches you the prompt is decorative.
    """
    if not drift.behind_origin:
        return False, "already up to date"
    if not drift.upstream:
        return False, "cannot reach origin"
    if drift.local.dirty:
        # `--ff-only` would refuse anyway; saying so up front names the fix.
        return False, "local tree has uncommitted changes — commit or stash first"
    return True, ""


def pull(root: Path | str, policy: UpdatePolicy) -> tuple[bool, str]:
    """`git pull --ff-only`. Returns (moved, message); never raises.

    Fast-forward only on purpose. A merge here would resolve someone's local
    work automatically on a machine nobody is watching, and a rebase would
    rewrite it. Refusing is the correct outcome for a divergent peer — it is
    a person's problem, and it says so.
    """
    if not (policy.auto_pull or policy.ask):
        return False, "updates are report-only (set auto_pull or ask)"
    before = local_revision(root).sha
    out = _git(root, "pull", "--ff-only", policy.remote, policy.branch)
    after = local_revision(root).sha
    if not out:
        return False, "pull failed (diverged, dirty, or no network)"
    if before == after:
        return False, "already up to date"
    return True, f"{before[:7]} → {after[:7]}"
