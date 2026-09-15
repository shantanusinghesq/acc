#!/usr/bin/env python3
"""Validate and atomically publish an excluded ACC draft."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path
from typing import List, Optional

try:
    from acc_archive import ArchiveLock, draft_parts, final_path_for_draft, validate_checkpoint
except ModuleNotFoundError:  # Imported by path from the repository test suite.
    from scripts.acc_archive import (
        ArchiveLock,
        draft_parts,
        final_path_for_draft,
        validate_checkpoint,
    )


def publish_draft(draft: Path) -> Path:
    """Publish validated bytes without exposing a partial or overwriting."""
    draft = Path(draft).resolve()
    if draft_parts(draft) is None or draft.parent == draft:
        raise ValueError("draft must be a direct archive child named _draft-NNN-DATE-topic.md")
    if not draft.is_file():
        raise FileNotFoundError(f"ACC draft not found: {draft}")

    final = final_path_for_draft(draft)
    temporary = draft.parent / f"_publishing-{secrets.token_hex(8)}.tmp"
    try:
        with ArchiveLock(draft.parent):
            body = draft.read_text(encoding="utf-8")
            errors = validate_checkpoint(body)
            if errors:
                raise ValueError("checkpoint is not ready to publish:\n- " + "\n- ".join(errors))
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            # Hard-link creation is atomic and refuses an existing destination.
            # The link and temporary file share the already-complete bytes.
            os.link(temporary, final)
            try:
                draft.unlink()
            except OSError as exc:
                print(
                    f"finalize_acc: published {final}, but could not remove draft {draft}: {exc}",
                    file=sys.stderr,
                )
            return final
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and publish an ACC draft.")
    parser.add_argument("draft", help="Path to _draft-NNN-YYYY-MM-DD-topic.md")
    args = parser.parse_args(argv)
    try:
        final = publish_draft(Path(args.draft))
    except (FileNotFoundError, UnicodeError, ValueError) as exc:
        print(f"finalize_acc: {exc}", file=sys.stderr)
        return 2
    except FileExistsError:
        print("finalize_acc: final checkpoint already exists; draft preserved", file=sys.stderr)
        return 3
    except TimeoutError as exc:
        print(f"finalize_acc: {exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"finalize_acc: publication failed; draft preserved: {exc}", file=sys.stderr)
        return 4
    print(final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
