# Packet protocol — how agents work here

This directory is a multi-year work skeleton. It is designed to be executed
slowly, one packet at a time, by agents weaker than the one who wrote it.
Every packet is self-contained on purpose: assume the agent has NOT read any
other document.

## 0. Before you touch anything

1. `mesh mem traps` — what has already bitten this fleet.
2. `git status` in the repo — re-measure NOW, not from memory. Uncommitted
   work comes first (see packet 000 of this track).
3. Read the repo's `AGENTS.md` / `CLAUDE.md` and this repo's `packets/README.md`.

## 1. Pick exactly ONE packet

Order: lowest `phase`, then lowest `id`, then `status: todo`. Never take two.

## 2. Claim it

Edit the packet frontmatter: `status: claimed`, add `claimed_by:` and
`claimed_at:`. Commit the claim on your work branch (see §6).

## 3. The gate is the spec

If you cannot write or run the packet's gate, STOP. Set `status: blocked`,
write why in the report. That is real information, not failure. Do not
improvise a different definition of done.

## 4. Execute

- Follow the steps in order. They are ordered deliberately.
- Run the packet's fast gate (tier-0) after every change; run the FULL gate
  before declaring done. loom does this automatically — prefer loom.
- Every packet lists tests you MUST ADD. A fix with no test that fails before
  the fix and passes after it is NOT done.

## 5. Report (mandatory)

Append `packets/reports/<ID>.md`: what you did, trimmed gate output, what
surprised you, every unknown you discovered (feed unknowns back into a new
packet proposal at the bottom of the report). If something hurt, write it
down: `mesh mem remember trap "<title>" "<body>"`.

## 6. Commit strategy

- Branch `agent/<ID>` from current HEAD. Commit EARLY and often — worktrees
  get wiped between sessions.
- Conventional commits, one logical change per commit. Reference the packet id.
- NEVER: `git add -A` (sweeps unrelated work), `git push --force`, history
  rewrites, branch deletion, committing `.env`/`praxis_auth*`/
  `supabase_praxis_client_*`, pushing `main`/`master` directly.
- Merge to the default branch ONLY when the full gate passes and a human or
  stronger agent has reviewed. Production deploys happen through the repo's
  own mechanism (see repo packets/README.md) — never by copying files around.

## 7. Done

`status: review` + report written. A reviewer independently re-runs the gate,
sets `status: done` (or back to `todo` with a note). Claimed packets older
than 7 days with no commits revert to `todo`.

## 8. Escalation

3 failed iterations with the SAME error → `blocked` with evidence. 3 errors
all different → keep going, you are making progress. When a strong model is
needed, that is rung 3: a single well-formed question with the evidence you
gathered — not a fresh restart.

## 9. Queue integration

```bash
mesh task submit "$(cat packets/<ID>-*.md)" --title <ID> --engine loom \
    --spec ~/dev/loom/specs/<matching>.loom.json --run      # checkable work
mesh task submit "..." --title ... --engine opencode --run  # then VERIFY effects
mesh task status --all                                      # the board
```

Never shell out to `opencode run` directly: rc=0 is not evidence of work
(local models sometimes print tool-calls as text), and the queue is what makes
work visible.
