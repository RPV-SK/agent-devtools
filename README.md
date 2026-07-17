# agent-devtools

Small, dependency-free tools that make AI coding agents (and humans) better at
verifying their own work. Extracted from a real multi-agent engagement on a
macOS photo-archive tool, generalized for any project.

Born from a simple observation: every one of these existed because an agent
got burned without it.

## The tools

| Tool | What it does | Why it exists |
|---|---|---|
| [`pytest-progress/`](pytest-progress/) | Streams the currently-running test + elapsed time to a file an orchestrator can watch | A "hung" 8-minute suite was actually a 16-minute crawl under machine load — killed twice for lack of one glance of visibility |
| [`mutprobe/`](mutprobe/) | Declared mutation probes with guaranteed snapshot restore — "would the tests fail under a broken implementation?" as a tool, not an art | ~30 hand-rolled sed-and-restore probes across one engagement; one `git checkout --` restore silently ate uncommitted work |
| [`fakedrive/`](fakedrive/) | Real mountable macOS volumes (hdiutil DMGs with real diskutil VolumeUUIDs) as test fixtures | "Blocked on physical hardware" for months — when a synthetic drive with a real UUID takes 2 seconds to mint |
| [`appdriver/`](appdriver/) | Native macOS window list / screenshot / input CLI (CGWindowList + CGEvent) | Agents could verify a web engine but never the actual native app window users see |
| [`skills/two-critic-slice/`](skills/two-critic-slice/) | An orchestration skill: build agent → two independent critics → verify-the-critic → batched fix round → serialized merge | Seven consecutive feature slices each had a real defect caught pre-merge by this pattern — including a cache that served the wrong photo and a comparison view that silently drifted crops |

## Philosophy

- **Verification is the product.** An agent's claim that something works is
  worth exactly as much as the gate that would have caught it being wrong.
- **Restore is sacred.** Any tool that mutates a working tree must restore it
  byte-identically, from its own snapshot, on every exit path — never from git.
- **Honest degradation.** When a permission or dependency is missing, say
  exactly what's missing and how to grant it, with a distinct exit code —
  never a silent no-op.
- **Opt-in everything.** Default runs stay hermetic and fast; the expensive
  or environment-touching paths sit behind explicit flags.

## License

MIT.
