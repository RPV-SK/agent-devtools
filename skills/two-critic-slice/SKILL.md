---
name: two-critic-slice
description: Run one feature slice through a full adversarial gauntlet — build packet → build agent → test-gate critic + change critic in parallel → verify the critics → one batched fix round → serialized rebase-gate-merge. Use for any non-trivial change, especially on security, data-integrity, or file-moving surfaces.
---

# two-critic-slice — an orchestration gauntlet for agent-built features

Battle-tested across seven consecutive feature PRs on a production codebase.
**Every single slice had a real defect caught pre-merge** by this pattern —
including a cache that served the wrong image after an index rebuild, a
side-by-side comparison view that silently drifted crops, and a saved-filter
feature that silently broadened to "everything" when a value went stale. Do
not skip stages because a slice "looks clean" — plausible-but-wrong is the
exact failure this kills.

## Inputs (fill these before launching anything)

- **feature** — one sentence + acceptance criteria.
- **owned files** — the exact set the builder may touch. For shared hotspot
  files, name the REGION (e.g. "the detail-overlay section of app.js") —
  region-scoped parallel edits merge cleanly; unscoped ones don't.
- **invariant** — what must survive (your project's sacred rules: e.g.
  "read-only against originals, never deletes").
- **non-scope** — what the builder must NOT build; adjacent problems get
  listed as residuals, not silently fixed.
- **gate** — fail-under-broken tests + full suite + your acceptance script,
  plus live UI verification for UI changes.

## Stage 1 — worktree + build packet

One slice = one `git worktree` off the default branch = one build agent.
Non-negotiable packet lines (each earned by a real incident):

- "Run the full suite **in the foreground to completion** — do not background
  it — and open the PR before ending your turn." (Two builders in one
  engagement stopped on a "will push once it's green" promise.)
- "Run ≥3 REAL mutation probes on your own tests (mutate → run → observe red
  → restore) and report the table." Snapshot-restore from memory/backup —
  **never `git checkout --`** over uncommitted work; that combination has
  silently destroyed WIP.
- "Commit the implementation before probing."
- If a browser/GUI pane exists, exactly ONE agent per fleet owns it; tell all
  siblings it's off-limits. UI verification means real clicks and keys
  against a served worktree with scratch state — never production data.
- "DO NOT MERGE. Report raw data: PR URL, per-gate results, probe table,
  files changed, residuals for the critics."

## Stage 2 — verify the artifact before spending critic effort

PR exists; diff confined to owned files; test-count arithmetic consistent.
Diff against the branch's TRUE base — a stacked or stale branch diffed
against a moved main shows phantom changes.

## Stage 3 — two critics, in parallel, different substrates

- **Test-gate critic** — owns the slice worktree. One question: *would this
  gate actually fail under a broken implementation?* It runs its OWN ≥4
  independent mutation probes (not the builder's), checks fixture realism
  against real production data formats (date-only fixtures testing a
  timestamped world prove nothing), and maps every claimed behavior to a
  pinning test. Must end with the worktree byte-clean at the PR head.
- **Change critic** — reads from GIT ONLY (`git show origin/<branch>:<path>`),
  never the worktree the test critic is mutating. One question: *what is the
  worst real defect?* Demands demonstration — a scratch-DB or scripted repro
  beats reasoning every time. Standing hunt list: invariant violations, SQL
  NULL/fan-out/attribution bugs, path confinement, locks held across slow
  work, XSS/escaping, renamed public identifiers, cross-branch collisions
  (`git merge-tree`), and cache keys that survive a rebuild of the thing
  they key on.

## Stage 4 — verify the critics, then ONE batched fix round

Spot-check each critic's load-bearing claims against the branch before
acting — critics earn trust per-claim, not wholesale. Batch BOTH critics'
findings into a single fix round sent back to the ORIGINAL builder (it still
has the code in context). Every fix ships with red-then-green proof: the new
test must demonstrably FAIL on the pre-fix commit.

## Stage 5 — serialized merge

One PR at a time: rebase onto current main → rerun the full gate on the
rebased tree → push --force-with-lease → post the two-critic evidence as a PR
comment → merge → rerun the gate on main → retire the worktree. If tooling
blocks the merge, hand it to the human with the evidence attached.

## Sizing guidance

- Trivial mechanical change: skip the critics, keep the gate.
- Normal feature: the full gauntlet, one critic pair.
- A change from an unreviewed source (another agent, an external PR): treat
  reconciliation as its own build stage first, then critic the RESULT.
- Anything touching irreversible operations: never skip, and have the change
  critic write the exploit before believing it doesn't exist.
