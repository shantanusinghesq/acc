# ACC Global Archive — `~/.claude/acc`

This directory is the **cross-project** ACC archive: lossy, high-signal session
checkpoints written with `--global`, shared across every project on this
machine. The location is overridable with the `ACC_GLOBAL_DIR` environment
variable; the SessionStart hook refuses locations outside your home directory
unless `ACC_GLOBAL_ALLOW_OUTSIDE_HOME=1` is set.

## Naming convention

```
NNN-YYYY-MM-DD-topic.md
```

- `NNN` — sequence number with a minimum width of three digits (`001`, `002`, …, `999`, `1000`). Highest numeric value = newest.
- `YYYY-MM-DD` — date the checkpoint was produced.
- `topic` — short kebab-case focus slug.

Tooling parses and orders the sequence numerically, so digit-width changes after
`999` do not affect which completed checkpoint is newest. Keep completed
filenames in the convention above; use the producer and finalizer rather than
renaming them.

## What lands here

Only drafts explicitly created with `new_acc.py --global` and published with
`finalize_acc.py` become entries here. Per-project `docs/acc/` archives are
separate and are never scanned into this one. Drafts created with
`new_acc.py --global` carry a `**Source project:**` line recording where they
were produced; the SessionStart hook announces unstamped completed entries as
coming from an unspecified project.

## Lifecycle

- **Producer** — `new_acc.py --topic <slug> --global` reserves an excluded
  `_draft-NNN-…md` here. After it is filled, `finalize_acc.py <draft-path>`
  validates and publishes the corresponding completed entry without overwriting.
- **Consumer** — `acc_session_start.py --global` (the SessionStart hook) falls
  back to this archive when the current project has no completed `docs/acc/`
  entries; the project archive always takes precedence. It names the recorded
  source project and frames cross-project content as background context that
  should continue only when it matches the current project.
  `find_latest_acc.py --global` and `list_acc.py --global` read this archive
  directly.

## Notes

- This `README.md`, `_draft-*.md` files, and recognizable incomplete legacy
  scaffolds are **excluded** from the "latest entry" search and archive listing.
- An abandoned draft continues to reserve its sequence. Correct and finalize it,
  or leave it excluded; do not rename incomplete content to a final filename.
- Checkpoints here travel between projects by design — keep the per-project
  default for anything that shouldn't.
