#!/usr/bin/env python3
"""Shared archive primitives for ACC producer and consumer scripts.

The archive remains a directory of portable Markdown files. This module adds
the small amount of coordination and parsing needed to make local concurrent
writers safe and to order numeric sequence identifiers correctly.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple

ENTRY_RE = re.compile(r"^(\d{3,})-(\d{4}-\d{2}-\d{2})-(.+)\.md$")
DRAFT_RE = re.compile(r"^_draft-(\d{3,})-(\d{4}-\d{2}-\d{2})-(.+)\.md$")
KNOWN_TOKENS = (
    "{{DATE}}",
    "{{FOCUS}}",
    "{{TOKENS_BEFORE}}",
    "{{TOKENS_AFTER}}",
)
SECTIONS = (
    "Decisions",
    "Current State",
    "Open Questions",
    "Rejected Approaches",
    "Next Actions",
)
EMPTY_SECTION_LINES = {
    "Decisions": {"-", "- D:"},
    "Current State": {"-"},
    "Open Questions": {"-", "- Q:"},
    "Rejected Approaches": {"-", "- X:"},
    "Next Actions": {"1."},
}


class ArchiveLock:
    """Bounded process-held lock for one archive on a local filesystem.

    The stable lock file is intentionally never unlinked. Removing it could
    let a later process lock a different file object while an earlier process
    still owns the original lock.
    """

    def __init__(self, acc_dir: Path, timeout: float = 5.0, poll: float = 0.05):
        self.path = Path(acc_dir) / ".acc.lock"
        self.timeout = timeout
        self.poll = poll
        self._handle: Optional[BinaryIO] = None

    def __enter__(self) -> "ArchiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        handle: Optional[BinaryIO] = None
        while True:
            try:
                handle = self.path.open("a+b")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                break
            except OSError:
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
                    handle = None
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out opening archive lock {self.path}"
                    ) from None
                time.sleep(self.poll)
        assert handle is not None
        while True:
            try:
                self._acquire(handle)
                self._handle = handle
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(
                        f"timed out waiting for archive lock {self.path}"
                    ) from None
                time.sleep(self.poll)

    @staticmethod
    def _acquire(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        try:
            self._release(self._handle)
        finally:
            self._handle.close()
            self._handle = None


def entry_parts(path: Path) -> Optional[Tuple[int, str, str]]:
    match = ENTRY_RE.match(Path(path).name)
    if not match:
        return None
    seq, date, slug = match.groups()
    return int(seq), date, slug


def draft_parts(path: Path) -> Optional[Tuple[int, str, str]]:
    match = DRAFT_RE.match(Path(path).name)
    if not match:
        return None
    seq, date, slug = match.groups()
    return int(seq), date, slug


def reserved_sequences(acc_dir: Path) -> List[int]:
    """Return every sequence claimed by a final entry or excluded draft."""
    if not Path(acc_dir).is_dir():
        return []
    sequences: List[int] = []
    for path in Path(acc_dir).iterdir():
        if not path.is_file() or path.is_symlink():
            continue
        parts = entry_parts(path) or draft_parts(path)
        if parts:
            sequences.append(parts[0])
    return sequences


def next_sequence(acc_dir: Path) -> int:
    sequences = reserved_sequences(acc_dir)
    return (max(sequences) if sequences else 0) + 1


def _section_bodies(text: str) -> Tuple[Dict[str, str], List[str]]:
    errors: List[str] = []
    positions: List[Tuple[int, str]] = []
    for section in SECTIONS:
        marker = f"## {section}"
        matches = [m.start() for m in re.finditer(rf"(?m)^{re.escape(marker)}\s*$", text)]
        if len(matches) != 1:
            errors.append(f"expected exactly one {marker!r} heading")
        elif matches:
            positions.append((matches[0], section))
    if len(positions) != len(SECTIONS):
        return {}, errors
    positions.sort()
    if tuple(section for _, section in positions) != SECTIONS:
        errors.append("checkpoint sections are not in canonical order")
        return {}, errors
    bodies: Dict[str, str] = {}
    for index, (start, section) in enumerate(positions):
        heading_end = text.find("\n", start)
        if heading_end == -1:
            heading_end = len(text)
        end = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        bodies[section] = text[heading_end:end].strip()
    return bodies, errors


def validate_checkpoint(text: str, max_words: int = 800) -> List[str]:
    """Validate structural readiness for publication, not factual quality."""
    errors: List[str] = []
    for token in KNOWN_TOKENS:
        if token in text:
            errors.append(f"unresolved template token {token}")
    if not re.search(r"(?m)^# Session Checkpoint(?:\s|$)", text):
        errors.append("missing Session Checkpoint title")
    focus = re.search(r"(?m)^\*\*Focus:\*\*\s*(.*?)\s*$", text)
    if focus is None or not focus.group(1):
        errors.append("missing checkpoint focus")

    bodies, section_errors = _section_bodies(text)
    errors.extend(section_errors)
    for section, body in bodies.items():
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines or all(line in EMPTY_SECTION_LINES[section] for line in lines):
            errors.append(f"section {section!r} is unfinished")

    section_words = len(re.findall(r"\S+", "\n".join(bodies.values())))
    if section_words > max_words:
        errors.append(
            f"checkpoint sections contain {section_words} words; maximum is {max_words}"
        )
    return errors


def is_recognizably_unfinished(path: Path) -> bool:
    """Conservatively identify legacy final-named scaffolds.

    Existing entries do not need to satisfy the new strict publication gate.
    Only unmistakable template tokens or canonical empty scaffold sections are
    excluded from inherited context.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return True
    if any(token in text for token in KNOWN_TOKENS):
        return True
    bodies, _ = _section_bodies(text)
    for section, body in bodies.items():
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines or all(line in EMPTY_SECTION_LINES[section] for line in lines):
            return True
    return False


def ordered_completed_entries(acc_dir: Path) -> List[Path]:
    """Completed, convention-named entries sorted by numeric sequence."""
    directory = Path(acc_dir)
    if not directory.is_dir():
        return []
    entries = [
        path
        for path in directory.glob("*.md")
        if path.is_file()
        and not path.is_symlink()
        and entry_parts(path)
        and not is_recognizably_unfinished(path)
    ]
    return sorted(entries, key=lambda path: (entry_parts(path)[0], path.name))


def find_latest_completed(acc_dir: Path) -> Optional[Path]:
    """Return the newest unambiguous completed entry.

    Two legacy files can claim the same numeric identifier. Treating either
    one as authoritative would make inherited context depend on a filename
    tie-break, so skip duplicated identifiers and fall back to the newest
    unique sequence. Callers can surface ``archive_diagnostics`` to explain
    the skipped files.
    """
    entries = ordered_completed_entries(acc_dir)
    counts: Dict[int, int] = {}
    for path in entries:
        parts = entry_parts(path)
        if parts is not None:
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    for path in reversed(entries):
        parts = entry_parts(path)
        if parts is not None and counts[parts[0]] == 1:
            return path
    return None


def archive_diagnostics(acc_dir: Path) -> List[str]:
    """Describe legacy archive ambiguity without changing user files."""
    directory = Path(acc_dir)
    if not directory.is_dir():
        return []
    completed_by_sequence: Dict[int, List[str]] = {}
    unfinished: List[str] = []
    for path in directory.glob("*.md"):
        if not path.is_file() or path.is_symlink():
            continue
        parts = entry_parts(path)
        if parts is None:
            continue
        if is_recognizably_unfinished(path):
            unfinished.append(path.name)
        else:
            completed_by_sequence.setdefault(parts[0], []).append(path.name)

    messages: List[str] = []
    for sequence in sorted(completed_by_sequence):
        names = sorted(completed_by_sequence[sequence])
        if len(names) > 1:
            messages.append(
                "duplicate ACC sequence "
                f"{sequence}: {', '.join(names)}; automatic latest selection skips this sequence"
            )
    for name in sorted(unfinished):
        messages.append(
            f"unfinished legacy checkpoint {name} is excluded from automatic loading"
        )
    return messages


def final_path_for_draft(draft: Path) -> Path:
    parts = draft_parts(draft)
    if parts is None:
        raise ValueError(f"not an ACC draft filename: {draft.name}")
    seq, date, slug = parts
    return draft.parent / f"{seq:03d}-{date}-{slug}.md"
