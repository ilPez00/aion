---
id: XX-000
track: <track>
phase: 0
status: todo
rung: 1
depends: []
gate: "<one line: the command(s) that must exit 0>"
commit: "branch agent/XX-000; conventional commits; per PROTOCOL §6"
claimed_by: 
claimed_at: 
---

# <Title>

## Why (the failure this prevents)
<What goes wrong in production or in maintenance if this is never done. One
paragraph, concrete.>

## Context
<Measured facts: files, paths, line numbers, current behaviour, traps from
mesh mem. If you cannot verify a fact, say so and verify it first.>

## Steps
1. <ordered, concrete, each step independently checkable>

## Tests you must add
- **Unit:** <framework, file, what each test asserts>
- **Integration:** <what the end-to-end test covers>
- A bug fix without a regression test is not done.

## Gate
```bash
<commands; every one must exit 0>
```

## Commit strategy
<Anything repo-specific beyond PROTOCOL §6.>

## Report
Append packets/reports/<ID>.md per PROTOCOL §5.

## Never
<Repo-specific red lines.>
