# acc

[![CI](https://github.com/haremantra/acc/actions/workflows/ci.yml/badge.svg)](https://github.com/haremantra/acc/actions/workflows/ci.yml)

A session checkpoint for Claude Code. Five sections, one file, replay-free resume.

## What it does

Long sessions end and the next thread starts cold. Auto-compaction summarizes away the load-bearing parts — the decisions, the things you tried and ruled out — so even a compacted replay loses the most expensive context. `acc` writes a session checkpoint at end-of-session that the next thread loads as inherited context. You resume from your reasoning, not from a replay of the transcript.

The checkpoint is at most 800 words in five fixed sections. A typical session goes from ~150k tokens to ~1k.

| Section | What goes here |
|---|---|
| Decisions | What got decided, and why |
| Current State | What exists right now (files, branches, versions, tests) |
| Open Questions | What's blocked |
| Rejected Approaches | What was tried and ruled out |
| Next Actions | Ordered, specific enough to execute |

That fourth section is the one most handoff tooling drops. Auto-compaction tends to summarize away "we tried X, it failed because Y", and then the next session re-tries X. `acc` captures that on purpose. Negative knowledge is the most expensive class of facts to rediscover, so it gets a first-class slot.

## The second bet

A Step 0 necessity gate that aborts production if a plain `HANDOFF.md` would carry the same load. Most session-handoff skills always produce. This one asks if it should. The worst thing a productivity tool can do is run on momentum and dilute its own archive, so the gate fires often by design.

## Other tools in this space

There are good ones (mem0, Zep, REMvisual/claude-handoff). They're mostly retention-maximalist: preserve as much of the session as possible, dossier-style. `acc` is the lossy-digest end of that trade-off. For a 500k-token session with buried load-bearing facts, the dossier approach is probably the right tool. For a one-page checkpoint that seeds the next thread, this one is.

## Three modes

- **Produce.** `/acc [focus]` extracts the five sections into an excluded `docs/acc/_draft-NNN-YYYY-MM-DD-topic.md`, then validates and publishes it as `docs/acc/NNN-YYYY-MM-DD-topic.md` in the project being worked on.
- **Consume.** `/acc invoke-last` loads the newest archive entry into a fresh session as inherited context.
- **Inspect.** `/acc doctor` diagnoses the current archive without changing its contents.

## Requirements

- Claude Code (any recent version)
- Git, for clone and updates
- Python 3.8+, stdlib only, nothing to `pip install`

## Install

Claude Code discovers skills at `~/.claude/skills/<name>/SKILL.md`. Clone this repo directly into that path.

### macOS / Linux

```bash
git clone https://github.com/haremantra/acc.git ~/.claude/skills/acc
```

### Windows (PowerShell)

```powershell
git clone https://github.com/haremantra/acc.git "$HOME\.claude\skills\acc"
```

### Windows (Command Prompt)

```cmd
git clone https://github.com/haremantra/acc.git "%USERPROFILE%\.claude\skills\acc"
```

### Alternative: clone elsewhere and symlink (development workflow)

Useful if you want the working tree in `~/code/` (or somewhere else) and just want to expose it to Claude Code through a link. From inside the clone, the bundled installer does the symlink and verifies it for you:

**macOS / Linux:**
```bash
git clone https://github.com/haremantra/acc.git ~/code/acc
cd ~/code/acc && ./install.sh          # or: make install  (add --copy to copy instead of symlink)
```

**Windows (PowerShell, run as Administrator or with Developer Mode on):**
```powershell
git clone https://github.com/haremantra/acc.git C:\code\acc
cd C:\code\acc; ./install.ps1           # add -Copy to copy instead of symlink
```

Remove it later with `make uninstall` (or just delete `~/.claude/skills/acc`).

## Verify

Open Claude Code in any project and run:

```
/acc
```

The skill will first decide whether the session even warrants an `acc` (that's the necessity gate). To load the most recent archived `acc` into a fresh session, run:

```
/acc invoke-last
```

If `/acc` isn't recognized, restart Claude Code and check that `SKILL.md` lives at the expected path: `~/.claude/skills/acc/SKILL.md` on macOS/Linux, or `%USERPROFILE%\.claude\skills\acc\SKILL.md` on Windows.

## Draft and publication lifecycle

Production uses an excluded draft so an interrupted write cannot become the next inherited checkpoint. `new_acc.py` prints the absolute path it reserved:

```bash
python scripts/new_acc.py --topic auth-rewrite --focus "auth middleware"
# fill the printed _draft-...md file, including every section and token estimate
python scripts/finalize_acc.py "/absolute/path/to/_draft-NNN-YYYY-MM-DD-topic.md"
```

Use `python3` for these commands on macOS/Linux. The finalizer validates the checkpoint structure, unresolved template tokens, and the 800-word limit before atomically publishing the corresponding `NNN-YYYY-MM-DD-topic.md`. Structural validation does not establish factual accuracy or prove that every important decision was preserved. The finalizer never overwrites an existing final. A validation or publication failure preserves the draft for correction, and archive readers continue using the newest earlier completed entry.

Atomic publication uses local-filesystem hard links. If the archive or snapshot directory is on a filesystem that does not support them, finalization fails with the draft preserved and the snapshot hook reports the failure without blocking compaction. The remediation tests exercised local NTFS on Windows; network-share coordination, other filesystems, power-loss durability, and POSIX-native behavior require their respective CI or host verification.

## Auto-load on session start (optional)

Typing `/acc invoke-last` every time is easy to forget. A `SessionStart` hook makes it automatic: every new session in a project that has a `docs/acc/` archive inherits the latest entry, no command needed. Copy `assets/session-start-settings.json` into the project's `.claude/settings.json` (merge it if the file already exists):

```jsonc
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          { "type": "command", "command": "python3 \"$HOME/.claude/skills/acc/scripts/acc_session_start.py\"", "timeout": 15 }
        ]
      }
    ]
  }
}
```

On Windows, use this exact command in the hook object: `"command": "python \"$HOME/.claude/skills/acc/scripts/acc_session_start.py\""`. Claude Code runs command hooks through Git Bash when available or PowerShell otherwise, and both expand `$HOME`; `%USERPROFILE%` is Command Prompt syntax and does not expand in either hook shell. The hook is exit-0-safe (a misfire never blocks startup) and stays silent in projects with no archive, so it's safe to set globally in `~/.claude/settings.json`. `matcher: "startup"` fires on new sessions only, not resumes.

## Auto-snapshot before compaction (optional)

The SessionStart hook covers one end of a session; compaction is the other. When a session approaches the context limit, Claude Code replaces its history with a summary — and if no checkpoint was written first, any later ACC is produced *from that summary* instead of from the full window. A `PreCompact` hook preserves an insurance copy: it copies the raw transcript to `docs/acc/_snapshots/` (gitignored — transcripts can contain secrets — and pruned to the newest 5) in the moment between "compaction decided" and "history rewritten", so a checkpoint can be reconstructed from full fidelity. Both automatic compaction and manual `/compact` create snapshots only. Copy `assets/pre-compact-settings.json` into the project's `.claude/settings.json`:

```jsonc
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "auto",
        "hooks": [
          { "type": "command", "command": "python3 \"$HOME/.claude/skills/acc/scripts/acc_pre_compact.py\"", "timeout": 10 }
        ]
      },
      {
        "matcher": "manual",
        "hooks": [
          { "type": "command", "command": "python3 \"$HOME/.claude/skills/acc/scripts/acc_pre_compact.py\"", "timeout": 10 }
        ]
      }
    ]
  }
}
```

On Windows, use `"command": "python \"$HOME/.claude/skills/acc/scripts/acc_pre_compact.py\""` for both matcher entries. Keep `$HOME` for either Git Bash or PowerShell. The hook emits no stdout. Missing input stays quiet; unexpected operational failures produce one bounded stderr diagnostic and exit 0 so they do not block compaction. Unknown flags and invalid values retain argparse's exit 2 behavior.

Snapshot retention uses recorded publication counters within each UTC second, independent of session and trigger names. Legacy snapshots remain eligible for retention; when old same-second names contain tied counters and therefore no recoverable chronology, the filename is the deterministic tie-break.

This snapshot-only design was verified against Claude Code 2.1.272 and the current [PreCompact](https://code.claude.com/docs/en/hooks#precompact) and [context-output](https://code.claude.com/docs/en/hooks#add-context-for-claude) contracts. PreCompact supports blocking compaction, but it is not listed among the events that inject `additionalContext` into the model.

## Browse the archive

Once an archive has more than a handful of completed entries:

```bash
python scripts/list_acc.py                 # dated, focus-labeled table, newest first
python scripts/list_acc.py --markdown      # Markdown table you can paste into a README index
```

Archive consumers order the numeric sequence, including after `999`, and ignore excluded drafts and recognizable incomplete legacy scaffolds. They report those scaffolds on stderr. If multiple completed legacy files claim one sequence, automatic latest selection reports the duplicate and skips that ambiguous sequence in favor of the newest unique sequence. This prevents abandoned, partially filled, or ambiguous checkpoints from becoming inherited context.

## Inspect archive health

Run `/acc doctor` in Claude Code, or invoke the helper directly:

```bash
python scripts/acc_doctor.py                       # current project's docs/acc
python scripts/acc_doctor.py --dir "path/to/archive"
python scripts/acc_doctor.py --global              # configured global archive only
python scripts/acc_doctor.py --json                # deterministic JSON, schema version 1
```

Use `python3` on macOS/Linux. The same options are accepted after `/acc doctor`.
`--dir` and `--global` are mutually exclusive. Doctor displays the actual inspected
location and applies the existing archive-selection rules without a global fallback.

The report identifies the latest unique completed checkpoint, duplicate completed
IDs, excluded drafts and unfinished finals, unreadable files, skipped links, and
structural validation findings. Drafts are informational. Strict-format warnings
on legacy checkpoints are reported separately from eligibility to load; doctor
does not tighten the existing readers' compatibility rules.

Exit codes are **0** for no warning/error findings (including missing archives and
informational drafts), **1** for warnings, and **2** for invalid invocation or
incomplete inspection. Automation should check both the exit code and the reported
archive status: a missing archive is not a healthy populated archive.

Doctor never creates directories or lockfiles, writes archive content, or prints
checkpoint bodies. It skips entry symlinks rather than reading their targets.
The inspection is not an atomic snapshot: another process may change the archive
while it runs. An incomplete inspection cannot certify which entry a later reader
will load. Doctor does not audit hook settings, transcript snapshots, factual
accuracy, write permissions, or hard-link support.

Inspection is bounded to 10,000 directory items, 2 MiB per final entry, and a
64 MiB cumulative read budget; exceeding a read limit produces an
incomplete-inspection result. At most 200 findings are displayed, with an omitted
count and status reflecting all findings.
See the [versioned output contract](references/doctor-output.md) for fields,
diagnostic codes, and automation guidance.

## Global vs per-project archive

By default every entry lands in `./docs/acc/` of the project you're in — checkpoints live with the code they describe. If you'd rather keep **one archive across all projects** (handy when you hop between many repos), pass `--global` to any of the scripts:

```bash
python scripts/new_acc.py --topic auth-rewrite --global   # prints the reserved _draft path
python scripts/finalize_acc.py "/absolute/path/to/_draft-NNN-YYYY-MM-DD-topic.md" # validates and publishes
python scripts/find_latest_acc.py --global                # newest entry in the global archive
python scripts/list_acc.py --global                       # browse the global archive
```

The global location is `~/.claude/acc`, overridable with the `ACC_GLOBAL_DIR` environment variable. `--global` and `--dir` are mutually exclusive. The global archive holds only entries explicitly created with `--global` and then published with `finalize_acc.py` — per-project `docs/acc/` directories are never scanned into it. Drafts created by `new_acc.py --global` are stamped with a `**Source project:**` line recording where they were produced; entries predating the stamp (or written by hand) lack it.

For the SessionStart hook, append `--global` to the command in `settings.json` to fall back to the shared archive. The project's own `docs/acc/` still takes precedence — the global archive is consulted only when the project has no checkpoints — and a globally-sourced checkpoint is injected with a preamble that names its source project (or says it came from an unspecified project when the entry carries no stamp) and frames it as background context rather than work to continue, so a session in project B can't be misdirected into continuing project A's work. Every archive read the hook makes under `--global` is logged to stderr, and a global archive outside your home directory is refused outright — a hostile workspace can set `ACC_GLOBAL_DIR`, so out-of-home locations load only if you opt in with `ACC_GLOBAL_ALLOW_OUTSIDE_HOME=1`.

Two trust notes. Checkpoints in the global archive travel between projects by design — keep the per-project default for anything that shouldn't. And the hook trusts a project's own `docs/acc/` the same way Claude Code trusts the rest of a repo's config (its `CLAUDE.md`, for example): opening a session in a repo means its checkpoints can be injected, in any hook mode — don't wire the hook for repos you don't trust.

## Layout

| Path | Purpose |
|---|---|
| `SKILL.md` | Skill definition: process steps, rules, bundled resources |
| `scripts/acc_archive.py` | Shared archive locking, parsing, numeric ordering, and checkpoint validation |
| `scripts/new_acc.py` | Reserve and scaffold an excluded draft (produce mode); `--dry-run` to preview |
| `scripts/finalize_acc.py` | Validate and atomically publish a completed draft without overwriting a final |
| `scripts/find_latest_acc.py` | Locate the highest numeric completed entry (consume mode) |
| `scripts/list_acc.py` | Print completed entries as an index (`--markdown` for a table) |
| `scripts/acc_doctor.py` | Read-only archive diagnostics and latest selection (`--json` for structured output) |
| `scripts/acc_session_start.py` | SessionStart hook: auto-load the latest entry into a fresh session |
| `scripts/acc_pre_compact.py` | PreCompact hook: save a snapshot-only insurance copy and retain the newest 5 |
| `assets/acc-template.md` | Canonical output skeleton |
| `assets/docs-acc-readme.md` | README seed dropped into `docs/acc/` on first run |
| `assets/global-acc-readme.md` | README seed dropped into the global archive on first run |
| `assets/session-start-settings.json` | Example `.claude/settings.json` for the hook |
| `assets/pre-compact-settings.json` | Example `.claude/settings.json` for the PreCompact hook |
| `references/necessity-check.md` | The Step 0 rubric, 9 criteria for `acc` vs. `HANDOFF` |
| `references/example-acc.md` | Good vs. bad worked example |
| `tests/` | Unit, skill-integrity, and usability tests (stdlib `unittest`) |
| `install.sh` / `install.ps1` / `Makefile` | One-command install + dev targets |
| `ruff.toml` | Lint/format config (CI's `lint` job) |

## Running the tests

The helper scripts are deterministic, so they're covered by a stdlib-only
test suite (no `pip` install needed). From the repo root:

```bash
python -m unittest discover -s tests
```

The suite covers the archive lifecycle as well as the individual helpers.
`test_scripts.py` pins slug generation, template substitution, archive
selection, numbering, and exit codes. `test_archive_lifecycle.py` exercises
draft exclusion, validation and atomic publication, numeric ordering across
digit boundaries, abandoned drafts, competing producers, and fresh-process
create/finalize/restart behavior. `test_usability.py` exercises the SessionStart hook
end-to-end — archive resolution and project-before-global precedence,
fail-open exit behavior, stderr provenance logging — and pins both
injected preamble formats (project-local vs. globally-sourced), since
that framing is part of the hook's security surface.
`test_pre_compact.py` covers snapshot creation, retention, and fail-open hook
behavior, while `test_installers.py` checks installer safety in temporary
fixtures.
`test_doctor.py` verifies archive diagnostics, legacy compatibility, JSON/CLI
behavior, and unchanged fixture contents after inspection.
`test_skill_integrity.py` checks the bundle itself —
expected flat SKILL.md frontmatter fields and quoted description shape, that every bundled file it advertises exists, and
that every `{{TOKEN}}` the scaffolder substitutes is present in the
template (so a rename never ships a literal `{{DATE}}` to users).
Actual Claude loader acceptance requires validation by the installed host.

CI also runs `ruff check`, `ruff format --check`, and `compileall` over
`scripts/` and `tests/`; reproduce that locally with:

```bash
ruff check . && ruff format --check .
```

## Update

**macOS / Linux:**
```bash
git -C ~/.claude/skills/acc pull
```

**Windows (PowerShell):**
```powershell
git -C "$HOME\.claude\skills\acc" pull
```

## Uninstall

**macOS / Linux:**
```bash
rm -rf ~/.claude/skills/acc
```

**Windows (PowerShell):**
```powershell
Remove-Item -Recurse -Force "$HOME\.claude\skills\acc"
```

## Troubleshooting

**`/acc` doesn't appear in Claude Code.**
Confirm the file exists at the expected path:
- macOS/Linux: `ls ~/.claude/skills/acc/SKILL.md`
- Windows (PowerShell): `Get-ChildItem "$HOME\.claude\skills\acc\SKILL.md"`

Then restart Claude Code.

**Windows: helper scripts fail with `python3: command not found` or open the Microsoft Store.**
On Windows, invoke the scripts as `python new_acc.py ...`, not `python3 ...`. The `python3` alias often resolves to the Microsoft Store install shim, which fails silently. Either remove the shim from PATH or use the `python` launcher.

**`fatal: detected dubious ownership` when running `git` against the repo.**
This shows up on filesystems that don't record ownership (FAT/exFAT, some external drives, network shares). Mark the directory safe:

```bash
git config --global --add safe.directory <absolute-path-to-repo>
```

For a one-off invocation without touching global config:

```bash
git -c safe.directory=<absolute-path-to-repo> <command>
```

**`acc` entries are landing in the wrong project.**
By default the scripts create drafts in `./docs/acc/` relative to the current working directory at the moment the skill runs — not in the global archive unless `--global` was passed (see "Global vs per-project archive" above). If drafts end up in the wrong place, check Claude Code's working directory before filling or finalizing them.

**A new checkpoint does not appear in `invoke-last` or `list_acc.py`.**
`new_acc.py` creates an excluded `_draft-…md` file. Fill every section and replace every template token, then run `python scripts/finalize_acc.py <draft-path>` (`python3` on macOS/Linux). If validation fails, the command reports the defects and preserves the draft for correction.

## When not to use this

If your handoffs already work for you, you don't need this. That's what the gate is for.

## License

MIT. See `LICENSE`.
