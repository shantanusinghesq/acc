#!/usr/bin/env python3
"""Claude Code PreCompact hook: snapshot the raw transcript before compaction.

Compaction replaces a session's history with a machine-written summary. If a
checkpoint (`/acc` Mode A) hasn't been written by then, the full-fidelity
window is gone and any later checkpoint is a compression of that summary.
This hook fires in the gap between "compaction decided" and "history
rewritten" and copies the raw transcript (`transcript_path` from the hook's
stdin payload) to `<cwd>/docs/acc/_snapshots/`, so a checkpoint can still be
produced from the full window after compaction. Snapshots are pruned to the
newest few, and a `.gitignore` is seeded (re-seeded whenever absent) so they
are never committed — transcripts can contain secrets. The gitignore protects
the git channel only: if the project tree is zipped, synced, or shared by
other means, the snapshots travel with it. Point `--dest` somewhere outside
the tree (e.g. under `~/.claude/`) if that matters for your project.

The hook deliberately emits no stdout. The documented PreCompact contract does
not support injecting `additionalContext` into the model. `--snapshot-only`
remains accepted as a compatibility no-op for existing settings.

Mechanics worth knowing:

  * Snapshot names are `<UTCstamp>-<trigger>-<session8>-<NN>.jsonl`. A
    process-held directory lock reserves a unique same-second counter.
  * Data lands in a uniquely named `.part` file and a hard link publishes
    the complete copy without overwriting an existing snapshot.
  * The same bounded lock serializes pruning and partial-file cleanup, so an
    active copy is never pruned. A dead process releases the OS lock and its
    partial file is removed by the next successful operation.

Like the SessionStart hook, it is deliberately fail-open: after argument
parsing, operational errors produce one bounded stderr diagnostic and exit 0
with no stdout, so a hook misfire cannot block compaction. Missing or malformed
input remains quiet. Unknown flags and invalid values still exit 2 with
argparse's usage message because a settings typo should surface. Both
`transcript_path` and `cwd` come from the harness's stdin payload — the same
trust channel SessionStart relies on.

Usage (manual / test):
    python acc_pre_compact.py --dest /tmp/snaps < payload.json
    python acc_pre_compact.py --snapshot-only < payload.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple

DEFAULT_KEEP = 5
DIAGNOSTIC_LIMIT = 300
LOCK_TIMEOUT_SECONDS = 1.0
LOCK_POLL_SECONDS = 0.025
SESSION_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9-]")

# The naming contract take_snapshot() writes and prune() trusts. Anything in
# the snapshot dir that doesn't match (user-parked files, foreign .jsonl) is
# never deleted and never counts against --keep.
SNAPSHOT_RE = re.compile(
    r"^(\d{8}T\d{6}Z)-(?:auto|manual|unknown)-[A-Za-z0-9-]+-(\d{2,})\.jsonl$"
)
PART_RE = re.compile(
    r"^\.acc-snapshot-\d{8}T\d{6}Z-(?:auto|manual|unknown)-"
    r"[A-Za-z0-9-]+-\d+\.[0-9a-f]{32}\.part$"
)

# Ignore everything in the snapshot dir except the .gitignore itself, so a
# tracked docs/acc/ can never accidentally commit raw transcripts.
GITIGNORE_BODY = "*\n!.gitignore\n"


class SnapshotLock:
    """Bounded process-held lock for one snapshot directory.

    The lock file remains in place permanently. Unlinking it could allow two
    processes to lock different file objects while both believe they own the
    same directory.
    """

    def __init__(
        self,
        dest_dir: Path,
        timeout: Optional[float] = None,
        poll: float = LOCK_POLL_SECONDS,
    ) -> None:
        self.path = Path(dest_dir) / ".snapshot.lock"
        self.timeout = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
        self.poll = poll
        self._handle: Optional[BinaryIO] = None

    def __enter__(self) -> "SnapshotLock":
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
                    raise TimeoutError("timed out opening snapshot lock") from None
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
                    raise TimeoutError("timed out waiting for snapshot lock") from None
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


def _payload_from_stdin() -> dict:
    """Claude Code passes a JSON payload on stdin (cwd, transcript_path, ...)."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _clean_trigger(raw: object) -> str:
    return raw if raw in ("auto", "manual") else "unknown"


def _clean_session_id(raw: object) -> str:
    if not isinstance(raw, str):
        return "session"
    cleaned = SESSION_ID_SAFE_RE.sub("", raw)[:8]
    return cleaned or "session"


def snapshot_stem(trigger: str, session_id: str, now: datetime) -> str:
    """`20260612T154233Z-auto-62a1011c` — take_snapshot appends `-NN.jsonl`.

    A naive `now` is treated as UTC (not reinterpreted as local time), so a
    caller passing `datetime.utcnow()` gets the stamp it expects.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{_clean_trigger(trigger)}-{_clean_session_id(session_id)}"


def ensure_gitignore(dest_dir: Path) -> None:
    """Seed `.gitignore` whenever absent; a user-modified file is left alone."""
    gitignore = dest_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_BODY, encoding="utf-8")


def take_snapshot(
    transcript: Path,
    dest_dir: Path,
    trigger: str,
    session_id: str,
    now: Optional[datetime] = None,
) -> Path:
    """Copy the transcript into `dest_dir`, never overwriting an earlier copy.

    A counter is reserved while holding the snapshot-directory lock. The copy
    is completed and synced in a uniquely named `.part` file, then published
    with a hard link. Hard-link creation is atomic and refuses to replace an
    existing name, so a killed or competing writer cannot expose a truncated
    file under a valid snapshot name.
    """
    with SnapshotLock(dest_dir):
        ensure_gitignore(dest_dir)
        _cleanup_parts_locked(dest_dir)
        stem = snapshot_stem(trigger, session_id, now or datetime.now(timezone.utc))
        stamp = stem.split("-", 1)[0]
        counter = _next_counter_locked(dest_dir, stamp)
        part = dest_dir / (
            f".acc-snapshot-{stem}-{os.getpid()}.{uuid.uuid4().hex}.part"
        )
        try:
            with transcript.open("rb") as source, part.open("xb") as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            while True:
                target = dest_dir / f"{stem}-{counter:02d}.jsonl"
                try:
                    # A hard link publishes the complete copy atomically and
                    # fails if a non-cooperating writer claimed the name.
                    os.link(part, target)
                    break
                except FileExistsError:
                    counter += 1
            return target
        finally:
            try:
                part.unlink()
            except OSError:
                pass


def _next_counter_locked(dest_dir: Path, stamp: str) -> int:
    counters = []
    for path in dest_dir.glob("*.jsonl"):
        if not path.is_file() or path.is_symlink():
            continue
        match = SNAPSHOT_RE.match(path.name)
        if match and match.group(1) == stamp:
            counters.append(int(match.group(2)))
    return (max(counters) if counters else 0) + 1


def _cleanup_parts_locked(dest_dir: Path) -> List[Path]:
    """Remove partial copies only while holding the directory lock."""
    removed = []
    for path in dest_dir.glob("*.part"):
        if not PART_RE.match(path.name):
            continue
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            continue
    return removed


def _snapshot_sort_key(path: Path) -> Tuple[str, int, str]:
    match = SNAPSHOT_RE.match(path.name)
    if match is None:
        return "", -1, path.name
    return match.group(1), int(match.group(2)), path.name


def prune(dest_dir: Path, keep: int) -> List[Path]:
    """Delete all but the newest `keep` snapshots; returns what was removed.

    Only files matching the naming contract (`SNAPSHOT_RE`) are candidates —
    the `.gitignore` and anything a user parks here are never deleted and
    never displace a real snapshot from the keep window. Stale `.part`
    leftovers from a killed copy are swept too. Each deletion tolerates
    per-file failures (Windows locks, AV scans) so one stuck file can't
    abort the rest.
    """
    with SnapshotLock(dest_dir):
        snapshots = sorted(
            (
                p
                for p in dest_dir.glob("*.jsonl")
                if p.is_file() and not p.is_symlink() and SNAPSHOT_RE.match(p.name)
            ),
            key=_snapshot_sort_key,
        )
        doomed = snapshots[:-keep] if keep > 0 else snapshots
        removed: List[Path] = []
        for path in doomed:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                continue
        removed.extend(_cleanup_parts_locked(dest_dir))
        return removed


def _display_path(target: Path, base: Path) -> str:
    """Relativize under the project so transcripts don't carry machine layout."""
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return str(target)


def _at_least_one(value: str) -> int:
    """argparse type for --keep: a config typo should surface, not vanish."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("--keep must be >= 1")
    return number


def _report_failure(stage: str, error: Exception) -> None:
    """Emit one bounded diagnostic without echoing paths or transcript data."""
    error_name = re.sub(r"[^A-Za-z0-9_]", "", type(error).__name__)[:64] or "Exception"
    message = (
        f"acc_pre_compact: {stage} failed ({error_name}); "
        "check transcript access, snapshot destination permissions, and free space. "
        "Compaction was not blocked."
    )
    print(message[:DIAGNOSTIC_LIMIT], file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="PreCompact hook: snapshot the transcript before compaction."
    )
    parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="Compatibility option; PreCompact always creates only a snapshot.",
    )
    parser.add_argument(
        "--keep",
        type=_at_least_one,
        default=DEFAULT_KEEP,
        help=f"How many snapshots to retain, minimum 1 (default {DEFAULT_KEEP}).",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Snapshot dir; relative paths are anchored to the payload cwd. "
        "Defaults to <cwd>/docs/acc/_snapshots.",
    )
    args = parser.parse_args(argv)

    try:
        payload = _payload_from_stdin()
        transcript_raw = payload.get("transcript_path")
        if not isinstance(transcript_raw, str):
            return 0  # Silent: nothing to snapshot.
        transcript = Path(transcript_raw)
        if not transcript.is_file():
            return 0

        cwd_raw = payload.get("cwd")
        base = Path(cwd_raw).resolve() if isinstance(cwd_raw, str) else Path.cwd()
        if args.dest:
            dest_dir = (base / args.dest).resolve()
        else:
            dest_dir = base / "docs" / "acc" / "_snapshots"

        trigger = _clean_trigger(payload.get("trigger"))
        target = take_snapshot(
            transcript, dest_dir, trigger, _clean_session_id(payload.get("session_id"))
        )
        display = _display_path(target, base)
        success = f"acc_pre_compact: snapshotted transcript to {display}"
        print(success[:DIAGNOSTIC_LIMIT], file=sys.stderr)

        try:
            prune(dest_dir, args.keep)
        except Exception as error:
            # Housekeeping is best-effort; the snapshot already landed.
            _report_failure("snapshot retention", error)
    except Exception as error:
        # Never let a hook failure block compaction or expose transcript data.
        _report_failure("snapshot creation", error)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
