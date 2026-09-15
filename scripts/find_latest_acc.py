#!/usr/bin/env python3
"""Find the newest ACC in docs/acc/ (Mode B helper for the /acc skill).

Finds completed docs/acc/NNN-*.md entries, orders their sequence numerically,
and prints the absolute path of the highest sequence.
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
from typing import List, Optional

try:
    from acc_archive import archive_diagnostics, find_latest_completed
except ModuleNotFoundError:  # Imported by path from the repository test suite.
    from scripts.acc_archive import archive_diagnostics, find_latest_completed


def global_dir() -> Path:
    """The cross-project archive: $ACC_GLOBAL_DIR if set, else ~/.claude/acc."""
    env = os.environ.get("ACC_GLOBAL_DIR")
    return Path(env) if env else Path.home() / ".claude" / "acc"


def find_latest(acc_dir: Path) -> Optional[Path]:
    return find_latest_completed(acc_dir)


def main(argv: Optional[List[str]] = None) -> int:
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

    for diagnostic in archive_diagnostics(acc_dir):
        print(f"find_latest_acc: {diagnostic}", file=sys.stderr)

    latest = find_latest(acc_dir)
    if latest is None:
        print(f"ACC archive {acc_dir} is empty - nothing to invoke.", file=sys.stderr)
        return 1

    print(latest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
