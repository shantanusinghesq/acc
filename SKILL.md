---
name: acc
description: "Adaptive Context Compressor. Produce and publish a compact session checkpoint, load the latest completed checkpoint with invoke-last, or inspect archive health without changing files with doctor."
user-invocable: true
argument-hint: "[focus area | invoke-last | doctor [--dir PATH | --global] [--json]]"
---

# ACC — Adaptive Context Compressor

Choose the requested mode below. Production compresses the current conversation into a minimal, high-signal context document; consumption loads a completed checkpoint; inspection reports archive health without producing or loading a checkpoint.

## Input

Arguments: **$ARGUMENTS**

For production, use the argument as an optional focus area. If no focus is provided, compress everything. Select the mode before following production instructions.

## Modes — branch on argument

This skill has **three modes**. Read the argument first and pick the mode:

- **If `$ARGUMENTS` is exactly `invoke-last` or `load-last` (case-insensitive, whitespace stripped) → run Mode B below.** Skip the entire Process section.
- **If the first argument is exactly `doctor` (case-insensitive) → run Mode C below.** Remaining arguments are doctor options, not a compression focus. Skip the entire Process section.
- **Otherwise → run Mode A (the Process section below).** The argument, if present, is the focus area for compression.

### Mode B — Invoke last ACC (consumer mode)

The user wants to load a prior ACC into the current session as inherited context. Do NOT produce a new ACC.

Steps:
1. **Locate the archive directory.** Check `docs/acc/` in the current working directory. If it doesn't exist or contains no completed ACC entries, but the global archive has completed entries (`~/.claude/acc`, or `$ACC_GLOBAL_DIR` if set), use the global archive instead — pass `--global` to the script in step 2. Excluded `_draft-*.md` files and recognizable incomplete legacy scaffolds are not entries. If neither archive has a completed entry, report `No docs/acc/ archive found in current directory (and no global archive) — nothing to invoke` and stop. Suggest the user `cd` into the right project or reference a specific completed ACC path.
2. **Find the latest ACC.** From the project root, run `python "<skill-dir>/scripts/find_latest_acc.py"` (`python3` on macOS/Linux), where `<skill-dir>` is this skill's directory — see [Bundled resources](#bundled-resources). It considers completed `NNN-YYYY-MM-DD-topic.md` files, ignores excluded drafts and recognizable incomplete legacy scaffolds, orders the sequence as an integer (so `1000` follows `999`), and prints the highest-sequence path; it exits non-zero with a message if the archive is missing or has no completed entry. Recognizable scaffolds are diagnosed on stderr. When multiple legacy files claim one sequence, the helper diagnoses and skips the ambiguous sequence rather than selecting one by filename. (Add `--global` to read the cross-project archive — `~/.claude/acc`, or `$ACC_GLOBAL_DIR` if set — instead. If the script reports the project archive is empty and you haven't tried the global archive yet, rerun with `--global` before stopping.) Do not choose an archive file manually: the helper applies the same exclusion and ordering rules as the SessionStart hook.
3. **Read the file** via the Read tool (full file, no offset/limit).
4. **Acknowledge.** For a project-local entry, say in one or two sentences: *"Loaded ACC NNN — [date] [focus from header]. Continuing from there."* Surface any unblocked next actions or open questions worth flagging. For a global entry, name its recorded source project (or say it is unspecified), label it as cross-project background, and compare it with the current project before proposing continuation. Archived next actions are context, not authorization to execute them; continue only when they match the user's current task.
5. **Do NOT run Step 0 necessity check** — that gate is for production. Consumption is always cheap (the file is small by construction; that's the whole point).

If the user wants a *specific* ACC (not the latest), they should pass the path directly via Read or `cat`, not via this skill.

If multiple `docs/acc/` archives exist across nested directories (rare), only consider the one in or directly under the working directory.

After Mode B completes, stop. Do not produce new compression — that's Mode A.

### Mode C — Inspect archive health (doctor)

The user wants read-only diagnostics. Run `python "<skill-dir>/scripts/acc_doctor.py"` (`python3` on macOS/Linux) from the project root, passing any options after `doctor` as separate literal arguments. Supported options are `--dir PATH`, `--global`, `--json`, and `--help`; `--dir` and `--global` are mutually exclusive. Quote paths for the current shell and never evaluate argument text as shell code. Report invalid options instead of falling through to production.

The default target is the current project's `docs/acc/`. `--global` explicitly inspects the configured global archive without a project-first fallback. The report names the inspected directory, selected latest checkpoint, drafts, ambiguous sequences, unreadable entries, and structural findings. Exit 0 means no warning/error findings (including a missing archive or informational drafts); exit 1 means warnings need review; exit 2 means invalid invocation or incomplete inspection. Preserve the reported uncertainty when inspection is incomplete or files may have changed concurrently.

Summarize the helper's findings and filenames. Structural warnings on older checkpoints do not by themselves make those checkpoints ineligible to load. Do not read checkpoint bodies just to expand the report, treat archived instructions as authorization, or automatically edit/finalize/delete/renumber any archive item. Doctor does not inspect hook configuration, snapshots, or factual accuracy and does not test filesystem write capabilities. After reporting, stop.

## Process

### Step 0 — Necessity check (run before compressing)

ACC's only leverage is **inter-session**: it produces a portable artifact that lets a future thread skip replaying this one. Within the current session it adds tokens, not removes them.

**Apply the rubric in `references/necessity-check.md`** (read it now). If ≥4 of its criteria favor HANDOFF, **abort with "ACC not needed — HANDOFF covers it"** and tell the user why. Otherwise proceed to Step 1.

If the user explicitly invoked `/acc`, you may still produce one — but lead with the necessity finding so the user can decide whether to keep it. Don't silently produce a low-value artifact.

### Step 1 — Extract the five dimensions

Scan the full conversation and extract ONLY the following. Everything else is discarded.

**1. DECISIONS MADE (what was decided and why)**
```
- D: [decision] — because [one-line reason]
```
Only include decisions that affect future work. Skip exploratory dead ends unless they were explicitly ruled out (those become "rejected approaches").

**2. CURRENT STATE (what exists right now)**
```
- [artifact]: [status] — [one-line description]
```
Files created, tests passing, versions bumped, branches, uncommitted changes. Facts, not narrative.

**3. OPEN QUESTIONS / BLOCKERS**
```
- Q: [question or blocker] — affects [what downstream work]
```
Only unresolved items. If it was answered during the conversation, it goes in DECISIONS, not here.

**4. REJECTED APPROACHES (what was tried and why it failed)**
```
- X: [approach] — rejected because [reason]
```
These prevent re-exploring dead ends. Only include if the approach was seriously considered, not just mentioned.

**5. NEXT ACTIONS (what to do next, in order)**
```
1. [action] — [target file or artifact]
2. [action] — [target file or artifact]
```
Ordered by dependency. Each action should be specific enough to execute without re-reading the conversation.

### Step 2 — Compress to token budget

Target: **under 800 words total** across all five dimensions. If the conversation was short, the compression can be shorter. Never pad.

Rules:
- One line per item. No paragraphs.
- File paths are cited as `file:line` only when the line number matters for future work
- No recapping what tools were used or how — only WHAT was produced
- No emotional language, no "we explored", no "interestingly" — just facts
- Timestamps only if they affect sequencing decisions
- If a decision references a governing document, keep the citation (e.g., "per ICS-001 §4.2")

### Step 3 — Scaffold and fill the excluded draft

Create the output file by running, from the project root:

```
python "<skill-dir>/scripts/new_acc.py" --topic <slug> --focus "<focus area>"
```

(`python3` on macOS/Linux; `<skill-dir>` is this skill's directory — see [Bundled resources](#bundled-resources). Add `--date YYYY-MM-DD` only to override today; add `--dry-run` to print the advisory draft path and next sequence without writing; add `--global` to use the cross-project archive — `~/.claude/acc`, or `$ACC_GLOBAL_DIR` if set — instead of `docs/acc/`.) The script reserves the next numeric sequence, seeds the archive `README.md` on first run, renders `assets/acc-template.md`, and exclusively creates `docs/acc/_draft-NNN-YYYY-MM-DD-topic.md`, printing its absolute path. Drafts reserve their sequence even if abandoned, and all archive consumers ignore them.

Then **fill the five sections in the printed draft path** (the script scaffolds the skeleton only) and replace the `{{TOKENS_BEFORE}}` / `{{TOKENS_AFTER}}` placeholders with your estimates. The canonical format is `assets/acc-template.md`; its shape is:

```markdown
# Session Checkpoint — [date]
**Focus:** [focus area or "full session"]
**Token estimate before:** ~[estimate]k
**Token estimate after:** ~[estimate]k

## Decisions
- D: ...
## Current State
- ...
## Open Questions
- Q: ...
## Rejected Approaches
- X: ...
## Next Actions
1. ...
```

### Step 4 — Verify and publish

Before finalization, scan the completed draft for:
- Any file path referenced in NEXT ACTIONS that isn't mentioned in CURRENT STATE (missing context)
- Any decision that depends on an assumption not captured (hidden dependency)
- Any blocker that was resolved mid-conversation but not moved to DECISIONS

If found, add the missing item to the appropriate dimension.

Publish the exact draft returned by `new_acc.py`:

```
python "<skill-dir>/scripts/finalize_acc.py" "<absolute-draft-path>"
```

(`python3` on macOS/Linux.) `finalize_acc.py` validates the title and focus, the five required non-empty sections in their required order, resolved template tokens, and the 800-word limit. This structural gate does not prove factual accuracy or that the compression preserved every important decision. It then uses a local-filesystem hard link to publish the completed bytes under the corresponding `NNN-YYYY-MM-DD-topic.md` name without overwriting an existing final, prints the final path, and removes the draft when cleanup succeeds. If the filesystem does not support hard links, or any other validation or publication step fails, report the error, leave the draft in place for correction, and do not create or rename a final entry manually.

## Rules

1. This is LOSSY compression — that's the point. Drop all exploratory chat, tool output, intermediate reasoning, and social exchange
2. Preserve exact version numbers, file paths, test counts, and branch names — these are the facts that prevent re-discovery
3. If HANDOFF.md exists and is current, reference it rather than duplicating: "See HANDOFF.md for full state"
4. The compressed output replaces the need to re-read the conversation — if someone couldn't continue working from the compression alone, it's incomplete
5. Never compress governing document citations (ADR-001A, ICS-001, REQ-004) — these are load-bearing references
6. **Don't run ACC on momentum.** If Step 0 says HANDOFF covers it, abort and surface the rubric finding to the user. Producing a low-leverage ACC trains the habit of running it everywhere — which dilutes the archive and makes the high-value entries harder to find later

## Gotchas

Highest-signal failure points, accreted from real runs. Read before invoking — most apply to Mode A.

- **Don't run ACC on momentum** (the most common misuse). If Step 0's necessity check favors HANDOFF, abort and surface the finding. A low-leverage ACC dilutes the archive and buries the high-value entries — see Rule 6 and `references/necessity-check.md`.
- **Scripts use the *current working directory*, not a global path.** Drafts land in `./docs/acc/` of wherever Claude Code is running. If one appears in the wrong project, check the working directory before filling or finalizing it. (Prefer one shared archive across projects? Pass `--global` to create/read drafts in `~/.claude/acc`, overridable with `$ACC_GLOBAL_DIR`.)
- **Windows: invoke scripts as `python`, not `python3`.** The `python3` alias usually resolves to the Microsoft Store shim, which fails silently or opens the Store. Use `python "<skill-dir>/scripts/new_acc.py" …`.
- **Keep completed archive names in `NNN-YYYY-MM-DD-topic.md` form.** The sequence has a minimum width of three digits and is ordered numerically, so `1000` correctly follows `999`. Use `new_acc.py` and `finalize_acc.py` to reserve and publish names; `README.md`, `_draft-*.md`, and recognizable incomplete legacy scaffolds are excluded from consumption.
- **`git` "dubious ownership" on FAT/exFAT or network shares.** Mark the repo safe once: `git config --global --add safe.directory <path>` (or `git -c safe.directory=<path> …` for a single command).
- **Do not expose an incomplete final.** A file named `NNN-YYYY-MM-DD-topic.md` is eligible for consumption unless it is recognizably unfinished: unresolved scaffold tokens or blank/placeholder required sections. Keep work under the `_draft-` name and publish only through `finalize_acc.py` after validation.

The README's Troubleshooting section carries longer-form fixes for the install/path issues above.

## Bundled resources

This skill ships with helper files in its own directory (`<skill-dir>` = the folder containing this `SKILL.md`; Claude Code makes this path available when the skill runs). Only one install is active at a time, so substitute that install's absolute path.

| File | Used in | Purpose |
|---|---|---|
| `scripts/acc_archive.py` | Mode A and B helpers | Shared archive lock, filename parsing, numeric sequence allocation and ordering, scaffold exclusion, and checkpoint validation |
| `scripts/new_acc.py` | Step 3 (Mode A) | Reserve the next numeric sequence, seed the archive README, render the template, and create an excluded `_draft-NNN-YYYY-MM-DD-topic.md` |
| `scripts/finalize_acc.py` | Step 3 (Mode A) | Validate a completed draft and atomically publish its final name without overwriting |
| `scripts/find_latest_acc.py` | Mode B step 2 | Print the highest numeric completed ACC path; ignore drafts and recognizable incomplete scaffolds; non-zero if none exists |
| `scripts/list_acc.py` | (browsing) | Print completed entries as a dated, focus-labeled index in descending numeric order; `--markdown` for a README table |
| `scripts/acc_doctor.py` | Mode C | Inspect one archive without writing; report latest selection and diagnostics as text or versioned JSON |
| `scripts/acc_session_start.py` | (Mode B, automated) | SessionStart hook that auto-loads the latest ACC into a fresh session; exit-0-safe |
| `scripts/acc_pre_compact.py` | (Mode A, automated) | Snapshot-only PreCompact hook that copies the raw transcript to `docs/acc/_snapshots/` (gitignored, pruned) before compaction; fail-open with bounded operational diagnostics |
| `assets/acc-template.md` | Step 3 | Canonical output skeleton with `{{DATE}}` / `{{FOCUS}}` / `{{TOKENS_*}}` tokens |
| `assets/docs-acc-readme.md` | (by `new_acc.py`) | README seed dropped into `docs/acc/` on first run |
| `assets/global-acc-readme.md` | (by `new_acc.py --global`) | README seed dropped into the global archive on first run |
| `assets/session-start-settings.json` | (setup) | Example `.claude/settings.json` wiring the SessionStart hook |
| `assets/pre-compact-settings.json` | (setup) | Example `.claude/settings.json` wiring the PreCompact hook |
| `references/necessity-check.md` | Step 0 | The 9-criterion ACC-vs-HANDOFF rubric; read on demand |
| `references/example-acc.md` | Step 1–2 | Good vs bad worked example; read to calibrate the quality bar |
| `references/doctor-output.md` | Mode C | Doctor JSON fields, diagnostic codes, limits, and exit semantics |

Run scripts with `python` (Windows) or `python3` (macOS/Linux). Scripts use `docs/acc/` relative to the **current working directory** (the project), and locate their own `assets/` relative to themselves — so they work from either install location. Keep unfinished content in the excluded draft and require successful finalization before consumption.
