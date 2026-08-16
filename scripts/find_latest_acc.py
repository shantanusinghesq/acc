#!/usr/bin/env python3
"""Find the newest ACC in docs/acc/ (Mode B helper for the /acc skill).

Globs docs/acc/*.md (excluding README.md and _*.md extractor outputs), sorts
lexicographically, and prints the absolute path of the highest (newest) filename.
Exits non-zero with a clear message if the archive is missing or empty.

Usage:
    python find_latest_acc.py             # looks for ./docs/acc relative to cwd
    python find_latest_acc.py --dir PATH  # override the docs/acc directory
    python find_latest_acc.py --global    # the cross-project archive
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def global_dir() -> Path:
    """The cross-project archive: $ACC_GLOBAL_DIR if set, else ~/.claude/acc."""
    env = os.environ.get("ACC_GLOBAL_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude" / "acc"


def find_latest(acc_dir: Path) -> Path | None:
    if not acc_dir.is_dir():
        return None
    candidates = sorted(
        p
        for p in acc_dir.glob("*.md")
        if p.name.lower() != "readme.md" and not p.name.startswith("_")
    )
    return candidates[-1] if candidates else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find the newest ACC entry.")
    where = parser.add_mutually_exclusive_group()
    where.add_argument(
        "--dir",
        default=None,
        help="Path to the ACC archive directory (default: docs/acc under cwd).",
    )
    where.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="Use the cross-project archive (~/.claude/acc, or $ACC_GLOBAL_DIR).",
    )
    args = parser.parse_args(argv)

    acc_dir = (global_dir() if args.use_global else Path(args.dir or "docs/acc")).resolve()
    if not acc_dir.is_dir():
        print(f"No ACC archive found at {acc_dir} - nothing to invoke.", file=sys.stderr)
        return 1

    latest = find_latest(acc_dir)
    if latest is None:
        print(f"ACC archive {acc_dir} is empty - nothing to invoke.", file=sys.stderr)
        return 1

    print(latest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
