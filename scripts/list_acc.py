#!/usr/bin/env python3
"""List the ACC archive in docs/acc/ as a browsable index.

Globs docs/acc/*.md (excluding README.md), parses each entry's sequence
number and date from its filename and its focus from the file header, and
prints them newest-first. Useful once an archive has more than a handful of
entries and you want to scan or pick one without `ls`-ing raw filenames.

Usage:
    python list_acc.py                 # aligned text table, newest first
    python list_acc.py --markdown      # GitHub-flavored table (for a README index)
    python list_acc.py --dir PATH      # override the docs/acc directory
    python list_acc.py --global        # the cross-project archive
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

# NNN-YYYY-MM-DD-topic.md — the archive's naming contract.
ENTRY_RE = re.compile(r"^(\d{3,})-(\d{4}-\d{2}-\d{2})-(.+)\.md$")
# `**Focus:** the auth layer` header line in each entry.
FOCUS_RE = re.compile(r"^\*\*Focus:\*\*\s*(.+?)\s*$", re.MULTILINE)


def global_dir() -> Path:
    """The cross-project archive: $ACC_GLOBAL_DIR if set, else ~/.claude/acc."""
    env = os.environ.get("ACC_GLOBAL_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude" / "acc"


class Entry(NamedTuple):
    seq: str
    date: str
    slug: str
    focus: str
    path: Path


def _read_focus(path: Path, fallback: str) -> str:
    try:
        m = FOCUS_RE.search(path.read_text(encoding="utf-8"))
    except OSError:
        return fallback
    if not m:
        return fallback
    focus = m.group(1).strip()
    # Ignore the unrendered template placeholder.
    return focus if focus and focus != "{{FOCUS}}" else fallback


def parse_entries(acc_dir: Path) -> list[Entry]:
    """Return archive entries sorted newest-first (highest filename first)."""
    entries: list[Entry] = []
    if not acc_dir.is_dir():
        return entries
    for p in sorted(acc_dir.glob("*.md"), reverse=True):
        if p.name.lower() == "readme.md":
            continue
        m = ENTRY_RE.match(p.name)
        if not m:
            continue
        seq, date, slug = m.groups()
        entries.append(Entry(seq, date, slug, _read_focus(p, slug), p))
    return entries


def render_text(entries: list[Entry]) -> str:
    rows = [("ACC", "DATE", "FOCUS", "FILE")]
    rows += [(e.seq, e.date, e.focus, e.path.name) for e in entries]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    )


def render_markdown(entries: list[Entry]) -> str:
    lines = ["| ACC | Date | Focus | File |", "|---|---|---|---|"]
    for e in entries:
        lines.append(f"| {e.seq} | {e.date} | {e.focus} | [`{e.path.name}`]({e.path.name}) |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List the ACC archive as an index.")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--dir", default=None, help="Archive dir (default: docs/acc under cwd).")
    where.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="Use the cross-project archive (~/.claude/acc, or $ACC_GLOBAL_DIR).",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Emit a GitHub-flavored Markdown table instead of aligned text.",
    )
    args = parser.parse_args(argv)

    acc_dir = (global_dir() if args.use_global else Path(args.dir or "docs/acc")).resolve()
    if not acc_dir.is_dir():
        print(f"No ACC archive found at {acc_dir} - nothing to list.", file=sys.stderr)
        return 1

    entries = parse_entries(acc_dir)
    if not entries:
        print(f"ACC archive {acc_dir} has no entries yet.", file=sys.stderr)
        return 1

    print(render_markdown(entries) if args.markdown else render_text(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
