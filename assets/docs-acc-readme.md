# ACC Archive — `docs/acc/`

This directory holds **Adaptive Context Compressor** outputs: lossy, high-signal
compressions of past working sessions, produced by the `/acc` skill.

## Naming convention

```
NNN-YYYY-MM-DD-topic.md
```

- `NNN` — sequence number with a minimum width of three digits (`001`, `002`, …, `999`, `1000`). Highest numeric value = newest.
- `YYYY-MM-DD` — date the ACC was produced.
- `topic` — short kebab-case focus slug.

Tooling parses and orders the sequence numerically, so digit-width changes after
`999` do not affect which completed ACC is newest. Keep completed filenames in
the convention above; use the producer and finalizer rather than renaming them.

## Lifecycle

- **Producer (Mode A)** — `/acc [focus]` first reserves an excluded
  `_draft-NNN-…md`. It fills that draft, then `finalize_acc.py` validates and
  publishes the corresponding `NNN-…md` without overwriting an existing final.
- **Consumer (Mode B)** — `/acc invoke-last` loads the newest entry into a fresh
  session as inherited context, so you skip replaying prior conversation.

## Notes

- This `README.md`, `_draft-*.md` files, and recognizable incomplete legacy
  scaffolds are **excluded** from the "latest ACC" search and archive listing.
- An abandoned draft continues to reserve its sequence. Correct and finalize it,
  or leave it excluded; do not rename incomplete content to a final filename.
- Each entry is meant to stand alone: someone should be able to continue the work
  from the ACC alone, without re-reading the original conversation.
