#!/usr/bin/env python3
"""Tests for the PreCompact hook script (`acc_pre_compact.py`).

Stdlib-only (unittest), same as the rest of the suite:

    python -m unittest discover -s tests

The hook's contract has three load-bearing pieces these tests pin:
the snapshot is a faithful, atomic, never-overwriting copy whose names sort
in creation order (the prune ordering depends on it), with a seeded
`.gitignore` (raw transcripts must never be committable); pruning deletes
only contract-named snapshots and keeps the newest N; and the fail-open
behavior — no stdout, bounded diagnostics on operational errors, and silence
on malformed or absent input — so a misfire can never block compaction.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional, Tuple
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    """Import a script by path (the dir name `scripts` isn't a package)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pre_compact = _load("acc_pre_compact")

NOW = datetime(2026, 6, 12, 15, 42, 33, tzinfo=timezone.utc)


class SnapshotStemTests(unittest.TestCase):
    def test_format_is_stamp_trigger_session(self) -> None:
        self.assertEqual(
            pre_compact.snapshot_stem("auto", "62a1011c-086e", NOW),
            "20260612T154233Z-auto-62a1011c",
        )

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        # A naive datetime must not be reinterpreted as local time — a
        # shifted stamp would break chronological ordering against stamps
        # from the aware default path.
        naive = datetime(2026, 6, 12, 15, 42, 33)
        self.assertEqual(
            pre_compact.snapshot_stem("auto", "abc", naive),
            "20260612T154233Z-auto-abc",
        )

    def test_unknown_trigger_is_normalized(self) -> None:
        self.assertEqual(
            pre_compact.snapshot_stem("../evil", "abc", NOW),
            "20260612T154233Z-unknown-abc",
        )

    def test_session_id_is_sanitized_and_capped(self) -> None:
        # Unsafe chars stripped, then capped at 8.
        self.assertEqual(
            pre_compact.snapshot_stem("manual", "a/b\\c:d*e?f|g<h>i", NOW),
            "20260612T154233Z-manual-abcdefgh",
        )

    def test_non_string_session_id_falls_back(self) -> None:
        self.assertTrue(pre_compact.snapshot_stem("auto", None, NOW).endswith("-auto-session"))

    def test_lexicographic_order_is_chronological(self) -> None:
        earlier = pre_compact.snapshot_stem("auto", "s", NOW)
        later = pre_compact.snapshot_stem(
            "auto", "s", datetime(2026, 6, 12, 15, 42, 34, tzinfo=timezone.utc)
        )
        self.assertLess(earlier, later)


class TakeSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.transcript = self.tmp / "transcript.jsonl"
        self.transcript.write_text('{"turn": 1}\n', encoding="utf-8")
        self.dest = self.tmp / "snaps"

    def test_copies_content_and_creates_dirs(self) -> None:
        target = pre_compact.take_snapshot(self.transcript, self.dest, "auto", "abc", now=NOW)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"turn": 1}\n')
        self.assertEqual(target.parent, self.dest)

    def test_name_matches_the_prune_contract(self) -> None:
        # If the writer and the prune filter ever drift apart, snapshots
        # become unprunable (or worse, invisible to retention).
        target = pre_compact.take_snapshot(self.transcript, self.dest, "auto", "abc", now=NOW)
        self.assertEqual(target.name, "20260612T154233Z-auto-abc-01.jsonl")
        self.assertRegex(target.name, pre_compact.SNAPSHOT_RE)

    def test_no_part_file_left_behind(self) -> None:
        pre_compact.take_snapshot(self.transcript, self.dest, "auto", "abc", now=NOW)
        self.assertEqual(list(self.dest.glob("*.part")), [])

    def test_seeds_gitignore(self) -> None:
        pre_compact.take_snapshot(self.transcript, self.dest, "auto", "abc", now=NOW)
        gitignore = self.dest / ".gitignore"
        self.assertEqual(gitignore.read_text(encoding="utf-8"), "*\n!.gitignore\n")

    def test_user_modified_gitignore_untouched(self) -> None:
        self.dest.mkdir(parents=True)
        (self.dest / ".gitignore").write_text("# mine\n", encoding="utf-8")
        pre_compact.take_snapshot(self.transcript, self.dest, "auto", "abc", now=NOW)
        self.assertEqual((self.dest / ".gitignore").read_text(encoding="utf-8"), "# mine\n")

    def test_never_overwrites_and_collisions_sort_in_creation_order(self) -> None:
        first = pre_compact.take_snapshot(self.transcript, self.dest, "auto", "abc", now=NOW)
        self.transcript.write_text('{"turn": 2}\n', encoding="utf-8")
        second = pre_compact.take_snapshot(self.transcript, self.dest, "auto", "abc", now=NOW)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(encoding="utf-8"), '{"turn": 1}\n')
        self.assertEqual(second.read_text(encoding="utf-8"), '{"turn": 2}\n')
        # Load-bearing for prune: the newer same-second copy must sort AFTER
        # the older one, or retention deletes the wrong file.
        self.assertLess(first.name, second.name)
        self.assertRegex(second.name, pre_compact.SNAPSHOT_RE)


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self, *names: str) -> None:
        for name in names:
            (self.dir / name).write_text("x\n", encoding="utf-8")

    def test_keeps_newest_n(self) -> None:
        self._seed(
            "20260601T000000Z-auto-a-01.jsonl",
            "20260602T000000Z-auto-a-01.jsonl",
            "20260603T000000Z-auto-a-01.jsonl",
        )
        removed = pre_compact.prune(self.dir, keep=2)
        self.assertEqual([p.name for p in removed], ["20260601T000000Z-auto-a-01.jsonl"])
        survivors = sorted(p.name for p in self.dir.glob("*.jsonl"))
        self.assertEqual(
            survivors,
            ["20260602T000000Z-auto-a-01.jsonl", "20260603T000000Z-auto-a-01.jsonl"],
        )

    def test_same_second_collision_prunes_the_older_copy(self) -> None:
        # Regression: with a non-sorting collision suffix, retention would
        # delete the NEWER copy and keep the stale one.
        self._seed(
            "20260601T000000Z-auto-a-01.jsonl",
            "20260601T000000Z-auto-a-02.jsonl",
        )
        pre_compact.prune(self.dir, keep=1)
        survivors = [p.name for p in self.dir.glob("*.jsonl")]
        self.assertEqual(survivors, ["20260601T000000Z-auto-a-02.jsonl"])

    def test_under_limit_removes_nothing(self) -> None:
        self._seed("20260601T000000Z-auto-a-01.jsonl")
        self.assertEqual(pre_compact.prune(self.dir, keep=5), [])

    def test_only_contract_named_files_are_candidates(self) -> None:
        # A user-parked .jsonl must neither be deleted nor displace a real
        # snapshot from the keep window.
        self._seed(
            "20260601T000000Z-auto-a-01.jsonl",
            "20260602T000000Z-auto-a-01.jsonl",
            "notes.jsonl",
        )
        (self.dir / ".gitignore").write_text("*\n", encoding="utf-8")
        (self.dir / "keep.md").write_text("keep me\n", encoding="utf-8")
        pre_compact.prune(self.dir, keep=2)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(
            names,
            [
                ".gitignore",
                ".snapshot.lock",
                "20260601T000000Z-auto-a-01.jsonl",
                "20260602T000000Z-auto-a-01.jsonl",
                "keep.md",
                "notes.jsonl",
            ],
        )

    def test_stale_part_files_are_swept(self) -> None:
        self._seed("20260601T000000Z-auto-a-01.jsonl")
        part = self.dir / (".acc-snapshot-20260601T000000Z-auto-a-123." + ("a" * 32) + ".part")
        part.write_text("trunc", encoding="utf-8")
        pre_compact.prune(self.dir, keep=5)
        self.assertEqual(list(self.dir.glob("*.part")), [])
        self.assertTrue((self.dir / "20260601T000000Z-auto-a-01.jsonl").exists())

    def test_foreign_part_file_is_not_deleted(self) -> None:
        foreign = self.dir / "notes.part"
        foreign.write_text("mine", encoding="utf-8")
        old_pattern_lookalike = self.dir / (".foreign.123." + ("c" * 32) + ".part")
        old_pattern_lookalike.write_bytes(b"foreign bytes\x00must remain")
        pre_compact.prune(self.dir, keep=5)
        self.assertTrue(foreign.exists())
        self.assertEqual(old_pattern_lookalike.read_bytes(), b"foreign bytes\x00must remain")

    def test_counter_is_sorted_numerically_past_two_digits(self) -> None:
        self._seed(
            "20260601T000000Z-auto-a-99.jsonl",
            "20260601T000000Z-auto-a-100.jsonl",
        )
        pre_compact.prune(self.dir, keep=1)
        self.assertEqual(
            [path.name for path in self.dir.glob("*.jsonl")],
            ["20260601T000000Z-auto-a-100.jsonl"],
        )

    def test_contract_named_directory_does_not_displace_snapshot(self) -> None:
        real = "20260601T000000Z-auto-a-01.jsonl"
        directory = "20260602T000000Z-auto-a-02.jsonl"
        self._seed(real)
        (self.dir / directory).mkdir()

        self.assertEqual(pre_compact.prune(self.dir, keep=1), [])
        self.assertTrue((self.dir / real).is_file())
        self.assertTrue((self.dir / directory).is_dir())

    def test_contract_named_symlink_does_not_displace_snapshot(self) -> None:
        real = self.dir / "20260601T000000Z-auto-a-01.jsonl"
        real.write_text("real", encoding="utf-8")
        source = self.dir / "foreign.jsonl"
        source.write_text("foreign", encoding="utf-8")
        linked = self.dir / "20260602T000000Z-auto-a-02.jsonl"
        try:
            linked.symlink_to(source)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        self.assertEqual(pre_compact.prune(self.dir, keep=1), [])
        self.assertEqual(real.read_text(encoding="utf-8"), "real")
        self.assertTrue(linked.is_symlink())

    def test_a_stuck_file_does_not_abort_the_rest(self) -> None:
        self._seed(
            "20260601T000000Z-auto-a-01.jsonl",
            "20260602T000000Z-auto-a-01.jsonl",
            "20260603T000000Z-auto-a-01.jsonl",
        )
        real_unlink = Path.unlink

        def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self.name.startswith("20260601"):
                raise PermissionError("locked")
            real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky_unlink):
            removed = pre_compact.prune(self.dir, keep=1)
        # The locked file survives, but the other doomed file still went.
        self.assertEqual([p.name for p in removed], ["20260602T000000Z-auto-a-01.jsonl"])
        self.assertTrue((self.dir / "20260601T000000Z-auto-a-01.jsonl").exists())


class MainTests(unittest.TestCase):
    """End-to-end through main() with a fabricated harness payload."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.transcript = self.tmp / "session.jsonl"
        self.transcript.write_text('{"role": "user"}\n', encoding="utf-8")

    def _run(self, payload: object, argv: Optional[List[str]] = None) -> Tuple[int, str, str]:
        stdin = StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        out, err = StringIO(), StringIO()
        with mock.patch.object(pre_compact.sys, "stdin", stdin):
            with redirect_stdout(out), redirect_stderr(err):
                rc = pre_compact.main(argv or [])
        return rc, out.getvalue(), err.getvalue()

    def _payload(self, **overrides: object) -> dict:
        payload: dict[str, object] = {
            "session_id": "62a1011c-086e-4ac3",
            "transcript_path": str(self.transcript),
            "cwd": str(self.tmp),
            "hook_event_name": "PreCompact",
            "trigger": "auto",
        }
        payload.update(overrides)
        return payload

    def test_auto_trigger_snapshots_without_stdout(self) -> None:
        rc, out, err = self._run(self._payload())
        self.assertEqual(rc, 0)
        snaps = list((self.tmp / "docs" / "acc" / "_snapshots").glob("*.jsonl"))
        self.assertEqual(len(snaps), 1)
        self.assertEqual(out, "")
        self.assertIn("snapshotted transcript", err)
        # stderr carries the relative display path, not the machine layout.
        self.assertNotIn(str(self.tmp), err)

    def test_snapshot_only_remains_an_accepted_compatibility_option(self) -> None:
        rc, out, _ = self._run(self._payload(trigger="manual"), ["--snapshot-only"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        snaps = list((self.tmp / "docs" / "acc" / "_snapshots").glob("*.jsonl"))
        self.assertEqual(len(snaps), 1)
        self.assertIn("-manual-", snaps[0].name)

    def test_manual_trigger_without_flag_also_stays_silent(self) -> None:
        rc, out, _ = self._run(self._payload(trigger="manual"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_missing_transcript_is_silent_success(self) -> None:
        rc, out, err = self._run(self._payload(transcript_path=str(self.tmp / "gone.jsonl")))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        self.assertFalse((self.tmp / "docs").exists())

    def test_no_transcript_field_is_silent_success(self) -> None:
        rc, out, err = self._run({"cwd": str(self.tmp), "trigger": "auto"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_malformed_stdin_is_silent_success(self) -> None:
        rc, out, err = self._run("{not json")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_stdin_read_failure_is_reported_without_blocking(self) -> None:
        stdin = mock.Mock()
        stdin.isatty.return_value = False
        stdin.read.side_effect = OSError("sensitive input detail")
        out, err = StringIO(), StringIO()
        with mock.patch.object(pre_compact.sys, "stdin", stdin):
            with redirect_stdout(out), redirect_stderr(err):
                rc = pre_compact.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("snapshot creation failed (OSError)", err.getvalue())
        self.assertNotIn("sensitive input detail", err.getvalue())

    def test_prune_failure_is_reported_without_blocking(self) -> None:
        with mock.patch.object(pre_compact, "prune", side_effect=OSError("locked")):
            rc, out, err = self._run(self._payload())
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("snapshotted transcript", err)
        self.assertIn("snapshot retention failed (OSError)", err)
        self.assertLessEqual(max(map(len, err.splitlines())), pre_compact.DIAGNOSTIC_LIMIT)

    def test_unexpected_snapshot_failure_is_bounded_and_does_not_echo_error(self) -> None:
        secret = "transcript contents must not appear " + ("x" * 1000)
        with mock.patch.object(pre_compact, "take_snapshot", side_effect=PermissionError(secret)):
            rc, out, err = self._run(self._payload())
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("snapshot creation failed (PermissionError)", err)
        self.assertNotIn(secret, err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertLessEqual(len(err.rstrip("\n")), pre_compact.DIAGNOSTIC_LIMIT)

    def test_dest_override_wins_over_cwd(self) -> None:
        dest = self.tmp / "elsewhere"
        rc, _, _ = self._run(self._payload(), ["--dest", str(dest)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(list(dest.glob("*.jsonl"))), 1)
        self.assertFalse((self.tmp / "docs").exists())

    def test_relative_dest_is_anchored_to_payload_cwd(self) -> None:
        rc, _, _ = self._run(self._payload(), ["--dest", "snaps-here"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(list((self.tmp / "snaps-here").glob("*.jsonl"))), 1)

    def test_keep_is_enforced_through_main(self) -> None:
        dest = self.tmp / "snaps"
        dest.mkdir()
        # Epoch-dated seeds sort before any real clock, so the test can't
        # rot as wall time advances.
        for stamp in ("19700101T000000Z", "19700102T000000Z", "19700103T000000Z"):
            (dest / f"{stamp}-auto-old-01.jsonl").write_text("x\n", encoding="utf-8")
        rc, _, _ = self._run(self._payload(), ["--dest", str(dest), "--keep", "2"])
        self.assertEqual(rc, 0)
        survivors = sorted(p.name for p in dest.glob("*.jsonl"))
        self.assertEqual(len(survivors), 2)
        # The just-written snapshot is the newest and must survive.
        self.assertTrue(survivors[-1].endswith("-auto-62a1011c-01.jsonl"))

    def test_keep_zero_is_rejected_at_parse_time(self) -> None:
        # keep<=0 would delete the snapshot just taken — a config typo that
        # must surface (exit 2), not silently self-destruct the insurance.
        for value in ("0", "-1"):
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stderr(StringIO()):
                    pre_compact.main(["--keep", value])
            self.assertEqual(ctx.exception.code, 2)

    def test_unknown_flag_exits_2(self) -> None:
        # Config typos in settings.json should surface, not vanish.
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(StringIO()):
                pre_compact.main(["--no-such-flag"])
            self.assertEqual(ctx.exception.code, 2)


class SnapshotProcessSafetyTests(unittest.TestCase):
    """Exercise lock ownership and concurrent hooks in real processes."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.dest = self.tmp / "snaps"
        self.transcript = self.tmp / "session.jsonl"
        self.transcript.write_bytes(b'{"turn":1}\n' * 10000)

    @staticmethod
    def _wait_for(path: Path, process: subprocess.Popen, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if process.poll() is not None:
                raise AssertionError(f"lock holder exited early with {process.returncode}")
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for subprocess readiness")
            time.sleep(0.02)

    def _lock_holder(self, seconds: float, crash: bool = False) -> tuple[subprocess.Popen, Path]:
        ready = self.tmp / f"ready-{time.time_ns()}"
        code = "\n".join(
            [
                "import importlib.util, os, pathlib, sys, time",
                "spec = importlib.util.spec_from_file_location('pc_worker', sys.argv[1])",
                "module = importlib.util.module_from_spec(spec)",
                "spec.loader.exec_module(module)",
                "dest = pathlib.Path(sys.argv[2])",
                "ready = pathlib.Path(sys.argv[3])",
                "with module.SnapshotLock(dest, timeout=2.0):",
                "    part = dest / ('.acc-snapshot-20260601T000000Z-auto-active-123.' "
                "+ ('b' * 32) + '.part')",
                "    part.write_text('partial', encoding='utf-8')",
                "    ready.write_text('ready', encoding='utf-8')",
                "    time.sleep(float(sys.argv[4]))",
                "    if sys.argv[5] == 'crash':",
                "        os._exit(23)",
            ]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(SCRIPTS / "acc_pre_compact.py"),
                str(self.dest),
                str(ready),
                str(seconds),
                "crash" if crash else "clean",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._wait_for(ready, process)
        return process, ready

    def test_lock_timeout_fails_open_and_does_not_prune_active_part(self) -> None:
        holder, _ = self._lock_holder(2.0)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        payload = {
            "session_id": "same-session",
            "transcript_path": str(self.transcript),
            "cwd": str(self.tmp),
            "trigger": "auto",
        }
        stdin = StringIO(json.dumps(payload))
        out, err = StringIO(), StringIO()
        with mock.patch.object(pre_compact, "LOCK_TIMEOUT_SECONDS", 0.05):
            with mock.patch.object(pre_compact.sys, "stdin", stdin):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = pre_compact.main(["--dest", str(self.dest)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("snapshot creation failed (TimeoutError)", err.getvalue())
        self.assertLessEqual(len(err.getvalue().rstrip("\n")), pre_compact.DIAGNOSTIC_LIMIT)
        self.assertEqual(len(list(self.dest.glob("*.part"))), 1)
        self.assertEqual(list(self.dest.glob("*.jsonl")), [])
        holder.terminate()
        holder.wait(timeout=5)

    def test_crashed_writer_part_is_cleaned_after_os_releases_lock(self) -> None:
        holder, _ = self._lock_holder(0.05, crash=True)
        self.assertEqual(holder.wait(timeout=5), 23)
        self.assertEqual(len(list(self.dest.glob("*.part"))), 1)
        pre_compact.prune(self.dest, keep=5)
        self.assertEqual(list(self.dest.glob("*.part")), [])

    def test_part_cleanup_failure_preserves_successful_snapshot(self) -> None:
        real_unlink = Path.unlink

        def fail_part_unlink(path: Path, *args: object, **kwargs: object) -> None:
            if path.suffix == ".part":
                raise PermissionError("temporary file busy")
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_part_unlink):
            target = pre_compact.take_snapshot(self.transcript, self.dest, "auto", "abc", now=NOW)
        self.assertEqual(target.read_bytes(), self.transcript.read_bytes())
        self.assertEqual(len(list(self.dest.glob("*.part"))), 1)
        pre_compact.prune(self.dest, keep=5)
        self.assertEqual(list(self.dest.glob("*.part")), [])

    def test_actual_paused_copy_survives_housekeeping_then_crash_is_cleaned(self) -> None:
        ready = self.tmp / "copy-paused"
        release = self.tmp / "copy-release"
        code = "\n".join(
            [
                "import importlib.util, inspect, pathlib, sys, time",
                "spec = importlib.util.spec_from_file_location('pc_worker', sys.argv[1])",
                "module = importlib.util.module_from_spec(spec)",
                "spec.loader.exec_module(module)",
                "source, first = inspect.getsourcelines(module.take_snapshot)",
                "target = first + next(i for i, line in enumerate(source) "
                "if 'shutil.copyfileobj' in line)",
                "ready = pathlib.Path(sys.argv[4])",
                "release = pathlib.Path(sys.argv[5])",
                "def trace(frame, event, arg):",
                "    if (frame.f_code is module.take_snapshot.__code__ and event == 'line' "
                "and frame.f_lineno == target):",
                "        ready.touch()",
                "        while not release.exists():",
                "            time.sleep(0.01)",
                "    return trace",
                "sys.settrace(trace)",
                "module.take_snapshot(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), "
                "'auto', 'paused-session')",
            ]
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                code,
                str(SCRIPTS / "acc_pre_compact.py"),
                str(self.transcript),
                str(self.dest),
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._wait_for(ready, child)
        parts = list(self.dest.glob("*.part"))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].stat().st_size, 0)
        self.assertEqual(list(self.dest.glob("*.jsonl")), [])
        with self.assertRaises(TimeoutError):
            pre_compact.prune(self.dest, keep=1)
        self.assertTrue(parts[0].exists())
        child.terminate()
        child.communicate(timeout=5)
        pre_compact.prune(self.dest, keep=1)
        self.assertEqual(list(self.dest.glob("*.part")), [])
        self.assertEqual(list(self.dest.glob("*.jsonl")), [])

    def test_concurrent_hooks_publish_unique_complete_snapshots_and_keep_newest(self) -> None:
        payload_path = self.tmp / "payload.json"
        payload_path.write_text(
            json.dumps(
                {
                    "session_id": "same-session",
                    "transcript_path": str(self.transcript),
                    "cwd": str(self.tmp),
                    "trigger": "auto",
                }
            ),
            encoding="utf-8",
        )
        gate = self.tmp / "start"
        worker = "\n".join(
            [
                "import importlib.util, io, pathlib, sys, time",
                "spec = importlib.util.spec_from_file_location('pc_worker', sys.argv[1])",
                "module = importlib.util.module_from_spec(spec)",
                "spec.loader.exec_module(module)",
                "gate = pathlib.Path(sys.argv[4])",
                "while not gate.exists():",
                "    time.sleep(0.005)",
                "sys.stdin = io.StringIO(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8'))",
                "raise SystemExit(module.main(['--dest', sys.argv[3], '--keep', '3']))",
            ]
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    worker,
                    str(SCRIPTS / "acc_pre_compact.py"),
                    str(payload_path),
                    str(self.dest),
                    str(gate),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(12)
        ]
        gate.write_text("go", encoding="utf-8")
        results = [process.communicate(timeout=15) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0] * 12)
        self.assertTrue(all(stdout == "" for stdout, _ in results))
        self.assertTrue(
            all(
                max((len(line) for line in stderr.splitlines()), default=0)
                <= pre_compact.DIAGNOSTIC_LIMIT
                for _, stderr in results
            )
        )
        snapshots = sorted(self.dest.glob("*.jsonl"), key=pre_compact._snapshot_sort_key)
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(len({path.name for path in snapshots}), 3)
        expected = self.transcript.read_bytes()
        self.assertTrue(all(path.read_bytes() == expected for path in snapshots))
        self.assertEqual(list(self.dest.glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
