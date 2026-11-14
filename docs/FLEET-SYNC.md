# FLEET-SYNC — saving work across an agent fleet

A contract for one command that commits agent work, lands it on the remote, and
copies it to storage that is not the machine that wrote it. Implements the
policy in `~/.config/agents/SYNC.md`; the script is `~/.local/bin/fleet-sync`.
This file is the reusable part: it is written so the run can be rendered as a
`WorkflowRow` (see `src/aion/workflows.py`).

## Why a fleet needs one

Agents write far more than humans, and they almost never finish by saving.
The failure modes seen in practice: work left in a working tree on one node,
untracked files silently swept into a commit (`git add -A` and a leaked key),
two agents committing in one checkout and hitting `index.lock`, and local clones
drifting dozens of commits from `origin` while each node believes it is current.

Anthropic's *Redesigning source control* (2026-10-13) is the same problem at
10 000× the volume — 2 500 merges/day in May 2026, 30 000+/day by September —
solved by moving merges to a server and keeping content-addressed objects
instead of full copies. A three-machine fleet cannot adopt Scion, but the
lessons scale down:

- keep the **content-addressed object** (a git bundle), not a mirror of every
  working tree — bundles for 27 repos fit in 6.7 GB where the trees are 116 GB;
- treat **refs as the scarce resource**: short-lived agent branches, `main` and
  release tags only;
- **fetch before judging** ahead/behind, or a push is rejected for a reason the
  report cannot show;
- **serialize git metadata writes**. Worktrees isolate files, not `.git`, so two
  agents in one repository still race on `index.lock`.

## The command

```bash
fleet-sync                # commit, push, bundle, copy
fleet-sync --dry-run      # plan only
fleet-sync --no-push      # local commits
fleet-sync --no-backup    # git only
fleet-sync --force-backup # bundle without a mounted tier
```

Guarantees, and the reasons each exists:

| guarantee | because |
|---|---|
| commits tracked changes only (`git add -u`) | untracked work is where secrets and half-written scratch live |
| refuses a staged diff matching secret patterns | a leaked key in one commit survives every clone |
| pushes only owned remotes | no accidental upstream PRs from vendor clones |
| never `--force`, never rewrite, never delete | a wrong merge is recoverable; a rewritten history is not |
| one run at a time (`flock`) | concurrent runs are the `index.lock` collision |

## Reading a run

`commit`, `push`, `stale` (strictly behind origin — nothing of ours at risk, not
a failure), `DIVERGED` (ahead *and* behind — needs a deliberate rebase),
`READONLY` (archived remote), `NO-UPSTREAM`, `SECRET?`, `dirty-sub` (only a
submodule gitlink moved). Only the divergent and secret lines need a human.

## As a workflow row

```json
{
  "id": "wf-fleet-sync",
  "kind": "task",
  "title": "Save work: commit, push, mirror",
  "stage": "verify",
  "progress": 0.9,
  "agents": [{"name": "node", "status": "done", "progress": 1.0, "blocked_by": null}],
  "blocked_by": null,
  "next_action": "act",
  "counts": {"committed": 0, "pushed": 0, "bundled": 0, "diverged": 0},
  "pipeline": ["plan", "act", "verify", "done"]
}
```

Mapping: `plan` = `--dry-run`; `act` = `--no-backup` then full run; `verify` =
`git bundle verify` on one bundle per tier; `blocked` = `DIVERGED`/`SECRET?`;
`done` = every node reports the same md5 for the policy file.

## Installing on a node

```bash
install -m755 fleet-sync ~/.local/bin/fleet-sync
install -m644 SYNC.md  ~/.config/agents/SYNC.md
~/.local/bin/fleet-notes-propagate    # appends the pointer to each harness
```

Then a user timer (`fleet-sync.timer`, 30 min) makes it periodic rather than
remembered. A node with no mounted tier skips the mirror instead of filling its
root filesystem.
