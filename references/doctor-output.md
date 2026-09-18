# Doctor output contract (version 1)

`python scripts/acc_doctor.py --json` emits one JSON object on stdout. It is
deterministic for an unchanged archive and configuration; it contains no scan
timestamp or checkpoint bodies. Doctor performs no writes, creates, or deletions
in the archive; ordinary filesystem reads may update access-time metadata.
Names and paths are metadata and are escaped in JSON and text output. Invocation
errors use argparse's stderr usage output.

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1` |
| `archive` | Absolute inspected archive location |
| `status` | `healthy`, `missing`, `findings`, or `error` |
| `latest` | Filename of the highest unambiguous completed sequence, or `null` |
| `selection_complete` | Whether inspection completed without errors; this does not certify an atomic observation |
| `consistency` | `best_effort` |
| `counts` | Counts of observed `finals`, `drafts`, `load_eligible`, and `ignored` items |
| `entries` | Final and draft records with `name`, `kind`, `sequence`, and `load_eligible` |
| `findings` | Records with stable `code`, `severity`, `path` (filename or `null`), and explanatory `message` |
| `omitted_findings` | Count of additional findings withheld after the output limit |

Entry `load_eligible` is `true` for a completed, readable, unique sequence;
`false` for an excluded entry; and `null` when a read bound, unsupported regular
file reparse point, or detected change prevents classification. Strict structural
findings alone do not alter legacy eligibility. Draft bodies are not read: a draft's existence is informational,
and its completeness is assessed by the finalizer when explicitly invoked.

An empty existing archive is `healthy` with `latest: null`. A missing archive
is `missing` with `latest: null`; inspection does not create it. An error
suppresses `latest` and makes `selection_complete` false. Even a complete scan
can miss concurrent changes, including a file edited after it was inspected.
`latest` describes the observation, not a guarantee about a future read.

| Code | Severity | Meaning |
|---|---|---|
| `archive_missing` | info | No archive at the inspected location |
| `draft_present` | info | Excluded draft present |
| `entry_symlink` | warning | Entry symlink or Windows reparse point skipped |
| `entry_not_file` | warning | Checkpoint-shaped name is not a regular file |
| `entry_unreadable` | warning | Entry metadata or content could not be read |
| `entry_invalid_utf8` | warning | Completed entry cannot be decoded as UTF-8 |
| `unfinished_final` | warning | Recognizable unfinished final excluded by existing reader rules |
| `legacy_structure` | warning | Publication-format violations; eligibility reported independently |
| `duplicate_sequence` | warning | Multiple completed entries share a numeric identifier |
| `archive_unreadable` | error | Archive could not be resolved, inspected, or listed |
| `archive_not_directory` | error | Archive location exists but is not a directory |
| `archive_limit_exceeded` | error | More than 10,000 directory items |
| `entry_limit_exceeded` | error | Final exceeds the 2 MiB inspection limit |
| `archive_read_limit_exceeded` | error | The final cannot be inspected within the 64 MiB cumulative read budget |
| `archive_changed` | error | Concurrent change observed |
| `inspection_incomplete` | error | Checkpoint type could not be established, or a regular Windows reparse file cannot be safely inspected |

The report includes at most 200 findings. Additional findings still affect
status and exit code and increment `omitted_findings`; a truncated findings list
must not be interpreted as the complete inventory of problems. Entry records
remain available up to the directory limit. Messages are human-readable;
integrations should branch on codes and fields rather than message text.

The cumulative read budget reserves each attempted final's observed size before
reading; failed attempts conservatively consume that reservation. Files exceeding
the per-file limit are not opened. Directory enumeration stops after observing
one item beyond the directory limit and reports incomplete inspection.

Exit **0** corresponds to healthy/missing or informational results; **1** to
warnings; **2** to inspection failure or invalid arguments. `--help` exits **0**.
Doctor does not test write permissions or hard-link support, inspect hook
configuration or snapshots, or verify the factual content of checkpoints.
