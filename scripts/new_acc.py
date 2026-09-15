#!/usr/bin/env python3
"""Reserve and scaffold an excluded ACC draft (Mode A helper).

Computes the next zero-padded sequence number, renders the bundled
assets/acc-template.md (substituting {{DATE}} and {{FOCUS}}), and writes an
excluded docs/acc/_draft-NNN-YYYY-MM-DD-topic.md. finalize_acc.py validates
and publishes the completed draft under its final NNN filename. On first run
it also drops the README seed (assets/docs-acc-readme.md) as docs/acc/README.md.

The model fills the five body sections after this scaffolds the skeleton.

Global entries (--global) are additionally stamped with a `**Source project:**`
line recording the producing cwd, and the global archive is seeded with its own
README (assets/global-acc-readme.md) instead of the project-oriented one.

Usage:
    python new_acc.py --topic auth-rewrite
    python new_acc.py --topic auth-rewrite --focus "auth middleware" --date 2026-05-27
    python new_acc.py --topic auth-rewrite --global   # cross-project archive
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

try:
    from acc_archive import ArchiveLock, next_sequence
except ModuleNotFoundError:  # Imported by path from the repository test suite.
    from scripts.acc_archive import ArchiveLock, next_sequence

# Bundled assets sit next to this script's parent: <skill>/assets/
ASSETS = Path(__file__).resolve().parent.parent / "assets"
TEMPLATE = ASSETS / "acc-template.md"
README_SEED = ASSETS / "docs-acc-readme.md"
GLOBAL_README_SEED = ASSETS / "global-acc-readme.md"

def global_dir() -> Path:
    """The cross-project archive: $ACC_GLOBAL_DIR if set, else ~/.claude/acc."""
    env = os.environ.get("ACC_GLOBAL_DIR")
    return Path(env) if env else Path.home() / ".claude" / "acc"


def next_seq(acc_dir: Path) -> int:
    """Next number after completed entries and reserved excluded drafts."""
    return next_sequence(acc_dir)


def slugify(topic: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", topic.strip().lower()).strip("-")
    return s or "session"


def stamp_source(body: str, project: Path) -> str:
    """Record the producing project in a global entry, right after the Focus
    line, so the SessionStart hook can announce where a cross-project
    checkpoint came from."""
    lines = body.splitlines(keepends=True)
    at = next(
        (i + 1 for i, line in enumerate(lines) if line.startswith("**Focus:**")),
        1 if lines else 0,
    )
    lines.insert(at, f"**Source project:** {project}\n")
    return "".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Reserve and scaffold a new ACC draft.")
    parser.add_argument("--topic", required=True, help="Short focus slug, e.g. auth-rewrite.")
    parser.add_argument("--focus", default="", help="Human-readable focus (defaults to topic).")
    parser.add_argument("--date", default="", help="YYYY-MM-DD (default: today).")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--dir", default=None, help="Archive dir (default: docs/acc under cwd).")
    where.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="Use the cross-project archive (~/.claude/acc, or $ACC_GLOBAL_DIR).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the path that would be created (it includes the next NNN) without writing.",
    )
    args = parser.parse_args(argv)

    if not TEMPLATE.is_file():
        print(f"Template missing: {TEMPLATE}", file=sys.stderr)
        return 2

    try:
        date = (_dt.date.fromisoformat(args.date) if args.date else _dt.date.today()).isoformat()
    except ValueError:
        print(f"Invalid --date {args.date!r} (expected YYYY-MM-DD).", file=sys.stderr)
        return 2

    focus = args.focus.strip() or args.topic.strip()
    slug = slugify(args.topic)

    # next_seq tolerates a missing dir, so we can predict the draft path
    # before creating anything — required for an honest --dry-run.
    acc_dir = (global_dir() if args.use_global else Path(args.dir or "docs/acc")).resolve()
    seq = next_seq(acc_dir)
    out = acc_dir / f"_draft-{seq:03d}-{date}-{slug}.md"

    if args.dry_run:
        # No mkdir, README, lock, reservation, or write: only an advisory path.
        print(out)
        return 0

    body = TEMPLATE.read_text(encoding="utf-8")
    body = body.replace("{{DATE}}", date).replace("{{FOCUS}}", focus)
    if args.use_global:
        body = stamp_source(body, Path.cwd())

    try:
        acc_dir.mkdir(parents=True, exist_ok=True)
        with ArchiveLock(acc_dir):
            seq = next_seq(acc_dir)
            out = acc_dir / f"_draft-{seq:03d}-{date}-{slug}.md"
            # Seed the archive README on first run. A byte-identical old
            # project seed in the global archive is stale tool output.
            seed = GLOBAL_README_SEED if args.use_global else README_SEED
            readme = acc_dir / "README.md"
            stale = (
                args.use_global
                and readme.is_file()
                and README_SEED.is_file()
                and readme.read_text(encoding="utf-8")
                == README_SEED.read_text(encoding="utf-8")
            )
            if (not readme.exists() or stale) and seed.is_file():
                readme.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
            with out.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
    except FileExistsError:
        print(f"Refusing to overwrite existing draft {out}", file=sys.stderr)
        return 3
    except TimeoutError as exc:
        print(f"Could not reserve ACC draft: {exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"Could not create ACC draft: {exc}", file=sys.stderr)
        return 4

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
