#!/usr/bin/env python3
"""Claude Code SessionStart hook: auto-load the latest ACC as inherited context.

Wire this into a project's .claude/settings.json so a fresh session inherits
the most recent ACC without anyone remembering to run `/acc invoke-last`
(Mode B, automated). See the README's "Auto-load on session start" section.

Behavior:
  * Finds the highest numeric completed docs/acc/NNN-*.md; README.md,
    excluded drafts/extractor outputs, and recognizable scaffolds are skipped.
  * With --global, the project archive still takes precedence; the
    cross-project archive (~/.claude/acc, or $ACC_GLOBAL_DIR) is consulted
    only when the project has no entries. A globally-sourced checkpoint is
    injected with a preamble naming its source project (stamped by
    new_acc.py --global) and framed as background context, not as work to
    continue — so a session in one project can't be misdirected into
    continuing another project's work.
  * If an entry is found, prints the SessionStart JSON envelope with its
    content in `hookSpecificOutput.additionalContext`, which Claude Code
    injects into the session context. Project-local paths are shown relative
    to the project so transcripts don't carry the machine layout.
  * If there's no archive/entry, prints nothing (no context injected) — no
    "starting fresh" noise on every project that doesn't use ACC.
  * Reads from the global archive are surfaced on stderr, and an archive
    outside the home directory is refused outright (env vars can be set by
    a hostile workspace) unless ACC_GLOBAL_ALLOW_OUTSIDE_HOME=1 opts in —
    so a redirected archive is blocked, not just visible.

It is deliberately bulletproof: after argument parsing, any unexpected error
produces one bounded, content-free stderr diagnostic and exits 0 with no
stdout, so a hook misfire can never block session startup. Expected absent
input or archives remain quiet. (Unknown flags still exit 2 with argparse's usage message — the
flags live in settings.json, not in data, so a config typo should surface,
not vanish.) The cwd is taken from the hook's stdin payload (Claude Code
sends `{"cwd": ...}`), falling back to the process cwd; `--dir` overrides
both (used by tests).

Usage (manual / test):
    python acc_session_start.py --dir docs/acc
    python acc_session_start.py --global   # fall back to the cross-project archive
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

try:
    from acc_archive import archive_diagnostics, find_latest_completed
except ModuleNotFoundError:  # Imported by path from the repository test suite.
    from scripts.acc_archive import archive_diagnostics, find_latest_completed

# Cap how much we inject; ACCs are tiny by design (<800 words), but guard
# against a hand-edited monster file blowing up the context window.
MAX_BYTES = 16_000
DIAGNOSTIC_LIMIT = 300

# new_acc.py --global stamps this line into global entries so a cross-project
# consumer can say where a checkpoint came from.
SOURCE_RE = re.compile(r"^\*\*Source project:\*\*\s*(.+?)\s*$", re.MULTILINE)


def global_dir() -> Path:
    """The cross-project archive: $ACC_GLOBAL_DIR if set, else ~/.claude/acc."""
    env = os.environ.get("ACC_GLOBAL_DIR")
    return Path(env) if env else Path.home() / ".claude" / "acc"


def find_latest(acc_dir: Path) -> Optional[Path]:
    return find_latest_completed(acc_dir)


def _cwd_from_stdin() -> Optional[str]:
    """Claude Code passes a JSON payload on stdin including the project cwd."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return None
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) else None


def _display_path(latest: Path, base: Path) -> str:
    """Relativize paths under `base` (the project) so transcripts don't carry
    usernames or machine layout; anything outside — e.g. the global archive —
    stays absolute, because relative would be ambiguous there."""
    try:
        return latest.relative_to(base).as_posix()
    except ValueError:
        return str(latest)


def _is_under_home(path: Path) -> bool:
    try:
        path.relative_to(Path.home().resolve())
        return True
    except (ValueError, RuntimeError):
        # RuntimeError: Path.home() undeterminable — fail closed.
        return False


def _surface_global_read(gdir: Path) -> bool:
    """Make a (possibly redirected) global-archive read visible on stderr and
    decide whether to load from it. Outside the home directory the archive is
    refused — a hostile workspace can set $ACC_GLOBAL_DIR, and the hook's
    framing is a trusted channel — unless ACC_GLOBAL_ALLOW_OUTSIDE_HOME=1."""
    suffix = " (set by $ACC_GLOBAL_DIR)" if "ACC_GLOBAL_DIR" in os.environ else ""
    print(f"acc_session_start: reading global archive {gdir}{suffix}", file=sys.stderr)
    if _is_under_home(gdir):
        return True
    if os.environ.get("ACC_GLOBAL_ALLOW_OUTSIDE_HOME") == "1":
        print(
            f"acc_session_start: warning: {gdir} is outside your home directory; "
            "loading anyway because ACC_GLOBAL_ALLOW_OUTSIDE_HOME=1",
            file=sys.stderr,
        )
        return True
    print(
        f"acc_session_start: refusing to load {gdir}: it is outside your home "
        "directory and could be attacker-controlled — set "
        "ACC_GLOBAL_ALLOW_OUTSIDE_HOME=1 if you trust it",
        file=sys.stderr,
    )
    return False


def build_context(latest: Path, display: str, *, from_global: bool = False) -> str:
    body = latest.read_text(encoding="utf-8")
    # Look for the source stamp before truncation so a long entry can't hide it.
    source_match = SOURCE_RE.search(body) if from_global else None
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_BYTES:
        # Slice by bytes (not characters) so the cap holds for non-ASCII;
        # errors="ignore" drops a trailing partial multibyte char cleanly.
        body = encoded[:MAX_BYTES].decode("utf-8", errors="ignore")
        body += "\n\n[...truncated; open the file for the full entry]"
    if from_global:
        # The stamp value comes from the (possibly attacker-controlled) entry;
        # strip backticks so it cannot escape its code span and speak in the
        # hook's voice, and cap length so it cannot dominate the preamble.
        raw = source_match.group(1) if source_match else ""
        clean = raw.replace("`", "").strip()[:200]
        produced_in = f"`{clean}`" if clean else "an unspecified project"
        return (
            f"Inherited cross-project context, auto-loaded from the global ACC "
            f"archive ({display}) by the ACC SessionStart hook. This checkpoint "
            f"was produced in {produced_in}, which may not be the project you "
            f"are in now — treat it as background context, and only continue "
            f"its work if it clearly matches the current project. To reload "
            f"manually later, run `/acc invoke-last`.\n\n{body}"
        )
    return (
        f"Inherited context from a prior session, auto-loaded from "
        f"{display} by the ACC SessionStart hook. This is a "
        f"compressed, lossy checkpoint — continue the work from it rather than "
        f"replaying earlier conversation. To reload manually later, run "
        f"`/acc invoke-last`.\n\n{body}"
    )


def _report_archive_diagnostics(acc_dir: Path) -> None:
    for diagnostic in archive_diagnostics(acc_dir):
        print(f"acc_session_start: {diagnostic}", file=sys.stderr)


def _report_failure(error: Exception) -> None:
    error_name = re.sub(r"[^A-Za-z0-9_]", "", type(error).__name__)[:64] or "Exception"
    message = (
        f"acc_session_start: context loading failed ({error_name}); "
        "check checkpoint readability and archive permissions. Session startup was not blocked."
    )
    print(message[:DIAGNOSTIC_LIMIT], file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SessionStart hook: load latest ACC.")
    where = parser.add_mutually_exclusive_group()
    where.add_argument(
        "--dir",
        default=None,
        help="Archive dir. Defaults to <cwd>/docs/acc (cwd from stdin or process).",
    )
    where.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="Fall back to the cross-project archive (~/.claude/acc, or "
        "$ACC_GLOBAL_DIR) when the project archive has no entries.",
    )
    args = parser.parse_args(argv)

    try:
        from_global = False
        if args.dir is not None:
            base = Path(os.getcwd()).resolve()
            archive = Path(args.dir).resolve()
            _report_archive_diagnostics(archive)
            latest = find_latest(archive)
        else:
            base = Path(_cwd_from_stdin() or os.getcwd()).resolve()
            archive = base / "docs" / "acc"
            _report_archive_diagnostics(archive)
            latest = find_latest(archive)
            if args.use_global:
                if latest is not None:
                    print(
                        f"acc_session_start: using project archive "
                        f"{base / 'docs' / 'acc'} (takes precedence over the "
                        f"global archive)",
                        file=sys.stderr,
                    )
                else:
                    gdir = global_dir().resolve()
                    if _surface_global_read(gdir):
                        _report_archive_diagnostics(gdir)
                        latest = find_latest(gdir)
                        from_global = latest is not None

        if latest is None:
            return 0  # Silent: nothing to inherit.

        envelope = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": build_context(
                    latest, _display_path(latest, base), from_global=from_global
                ),
            }
        }
        print(json.dumps(envelope))
    except Exception as error:
        # Never let a hook failure block the session.
        _report_failure(error)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
