#!/usr/bin/env python3
"""Observable lifecycle and concurrency tests for ACC checkpoints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import acc_archive, acc_session_start, finalize_acc, find_latest_acc, list_acc, new_acc

REPO_ROOT = Path(__file__).resolve().parent.parent


def _scaffold(directory: Path, topic: str = "topic", focus: str = "") -> Path:
    output = StringIO()
    args = ["--dir", str(directory), "--topic", topic, "--date", "2026-09-14"]
    if focus:
        args += ["--focus", focus]
    with redirect_stdout(output):
        rc = new_acc.main(args)
    if rc != 0:
        raise AssertionError(f"scaffold failed with {rc}")
    return Path(output.getvalue().strip())


def _fill(draft: Path, label: str = "test") -> None:
    text = draft.read_text(encoding="utf-8")
    text = text.replace("{{TOKENS_BEFORE}}", "10").replace("{{TOKENS_AFTER}}", "1")
    text = text.replace("- D: \n", f"- D: Preserve {label}.\n")
    text = text.replace("## Current State\n- \n", f"## Current State\n- {label} is ready.\n")
    text = text.replace("- Q: \n", "- Q: None.\n")
    text = text.replace("- X: \n", "- X: None.\n")
    text = text.replace("## Next Actions\n1. \n", "## Next Actions\n1. Continue.\n")
    draft.write_text(text, encoding="utf-8")


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.directory = Path(self._tmp.name) / "docs" / "acc"
        self.addCleanup(self._tmp.cleanup)

    def test_draft_is_excluded_until_validated_and_published(self) -> None:
        old = self.directory / "001-2026-09-13-old.md"
        old.parent.mkdir(parents=True)
        old.write_text("# Session Checkpoint\n**Focus:** old\n", encoding="utf-8")
        draft = _scaffold(self.directory, "new")
        self.assertEqual(draft.name, "_draft-002-2026-09-14-new.md")
        self.assertEqual(find_latest_acc.find_latest(self.directory), old)
        self.assertEqual(acc_session_start.find_latest(self.directory), old)
        self.assertEqual([entry.path for entry in list_acc.parse_entries(self.directory)], [old])

        _fill(draft)
        final = finalize_acc.publish_draft(draft)
        self.assertEqual(final.name, "002-2026-09-14-new.md")
        self.assertFalse(draft.exists())
        self.assertEqual(find_latest_acc.find_latest(self.directory), final)
        self.assertEqual(acc_session_start.find_latest(self.directory), final)
        self.assertEqual(list_acc.parse_entries(self.directory)[0].path, final)

    def test_abandoned_drafts_reserve_their_sequences(self) -> None:
        first = _scaffold(self.directory, "first")
        second = _scaffold(self.directory, "second")
        self.assertEqual(first.name.split("-")[1], "001")
        self.assertEqual(second.name.split("-")[1], "002")

    def test_non_file_names_do_not_reserve_sequences(self) -> None:
        fake_final = self.directory / "999-2026-09-14-directory.md"
        fake_draft = self.directory / "_draft-1000-2026-09-14-directory.md"
        fake_final.mkdir(parents=True)
        fake_draft.mkdir()
        draft = _scaffold(self.directory, "real")
        self.assertEqual(draft.name, "_draft-001-2026-09-14-real.md")

    def test_symlink_is_not_loaded_or_used_as_a_reservation(self) -> None:
        self.directory.mkdir(parents=True)
        complete = self.directory / "001-2026-01-01-complete.md"
        complete.write_text("# Session Checkpoint\n**Focus:** complete\n", encoding="utf-8")
        source = self.directory / "external-content.txt"
        source.write_text("# Session Checkpoint\n**Focus:** linked\n", encoding="utf-8")
        linked = self.directory / "999-2026-01-02-linked.md"
        try:
            linked.symlink_to(source)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        self.assertEqual(find_latest_acc.find_latest(self.directory), complete)
        draft = _scaffold(self.directory, "next")
        self.assertEqual(draft.name, "_draft-002-2026-09-14-next.md")

    def test_invalid_draft_is_preserved(self) -> None:
        draft = _scaffold(self.directory)
        errors = acc_archive.validate_checkpoint(draft.read_text(encoding="utf-8"))
        self.assertTrue(any("unresolved template token" in error for error in errors))
        with self.assertRaises(ValueError):
            finalize_acc.publish_draft(draft)
        self.assertTrue(draft.is_file())
        self.assertEqual(find_latest_acc.find_latest(self.directory), None)

    def test_explicit_none_entries_are_valid(self) -> None:
        draft = _scaffold(self.directory)
        _fill(draft)
        text = draft.read_text(encoding="utf-8")
        self.assertEqual(acc_archive.validate_checkpoint(text), [])

    def test_existing_final_is_never_overwritten(self) -> None:
        draft = _scaffold(self.directory)
        _fill(draft, "draft content")
        final = acc_archive.final_path_for_draft(draft)
        final.write_text("sentinel\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            finalize_acc.publish_draft(draft)
        self.assertEqual(final.read_text(encoding="utf-8"), "sentinel\n")
        self.assertTrue(draft.is_file())

    def test_numeric_order_crosses_digit_boundaries(self) -> None:
        self.directory.mkdir(parents=True)
        names = (
            "999-2026-01-01-a.md",
            "1000-2026-01-02-b.md",
            "9999-2026-01-03-c.md",
            "10000-2026-01-04-d.md",
        )
        for name in names:
            (self.directory / name).write_text(
                "# Session Checkpoint\n**Focus:** x\n", encoding="utf-8"
            )
        self.assertEqual(find_latest_acc.find_latest(self.directory).name, names[-1])
        self.assertEqual(acc_session_start.find_latest(self.directory).name, names[-1])
        self.assertEqual(
            [entry.seq for entry in list_acc.parse_entries(self.directory)],
            ["10000", "9999", "1000", "999"],
        )

    def test_recognizable_legacy_scaffold_falls_back(self) -> None:
        self.directory.mkdir(parents=True)
        complete = self.directory / "001-2026-01-01-complete.md"
        complete.write_text("# Session Checkpoint\n**Focus:** complete\n", encoding="utf-8")
        unfinished = self.directory / "002-2026-01-02-unfinished.md"
        unfinished.write_text(
            "# Session Checkpoint\n**Focus:** x\n**Token estimate before:** "
            "~{{TOKENS_BEFORE}}k\n",
            encoding="utf-8",
        )
        self.assertEqual(find_latest_acc.find_latest(self.directory), complete)

    def test_blank_section_in_legacy_checkpoint_falls_back(self) -> None:
        self.directory.mkdir(parents=True)
        complete = self.directory / "001-2026-01-01-complete.md"
        complete.write_text("# Session Checkpoint\n**Focus:** complete\n", encoding="utf-8")
        blank = self.directory / "002-2026-01-02-blank.md"
        blank.write_text(
            "# Session Checkpoint\n**Focus:** blank\n"
            "## Decisions\n- D: decided\n"
            "## Current State\n\n"
            "## Open Questions\n- Q: None.\n"
            "## Rejected Approaches\n- X: None.\n"
            "## Next Actions\n1. Continue.\n",
            encoding="utf-8",
        )
        self.assertTrue(acc_archive.is_recognizably_unfinished(blank))
        self.assertEqual(find_latest_acc.find_latest(self.directory), complete)

    def test_temp_cleanup_failure_does_not_turn_publication_into_failure(self) -> None:
        draft = _scaffold(self.directory, "cleanup")
        _fill(draft, "cleanup behavior")
        real_unlink = Path.unlink

        def fail_temporary_unlink(path: Path, *args: object, **kwargs: object) -> None:
            if path.name.startswith("_publishing-"):
                raise PermissionError("temporary file busy")
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_temporary_unlink):
            final = finalize_acc.publish_draft(draft)
        self.assertTrue(final.is_file())
        self.assertIn("cleanup behavior", final.read_text(encoding="utf-8"))
        self.assertFalse(draft.exists())

    def test_duplicate_sequence_is_diagnosed_and_skipped(self) -> None:
        self.directory.mkdir(parents=True)
        previous = self.directory / "001-2026-01-01-previous.md"
        duplicate_a = self.directory / "002-2026-01-02-alpha.md"
        duplicate_b = self.directory / "002-2026-01-03-beta.md"
        for path in (previous, duplicate_a, duplicate_b):
            path.write_text("# Session Checkpoint\n**Focus:** complete\n", encoding="utf-8")

        diagnostics = acc_archive.archive_diagnostics(self.directory)
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("duplicate ACC sequence 2", diagnostics[0])
        self.assertIn(duplicate_a.name, diagnostics[0])
        self.assertIn(duplicate_b.name, diagnostics[0])
        self.assertEqual(find_latest_acc.find_latest(self.directory), previous)
        self.assertEqual(acc_session_start.find_latest(self.directory), previous)

        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = find_latest_acc.main(["--dir", str(self.directory)])
        self.assertEqual(rc, 0)
        self.assertEqual(Path(stdout.getvalue().strip()), previous)
        self.assertIn("automatic latest selection skips this sequence", stderr.getvalue())

    def test_unfinished_legacy_checkpoint_has_visible_diagnostic(self) -> None:
        self.directory.mkdir(parents=True)
        unfinished = self.directory / "001-2026-01-01-scaffold.md"
        unfinished.write_text(
            "# Session Checkpoint\n**Focus:** {{FOCUS}}\n## Decisions\n- D:\n",
            encoding="utf-8",
        )
        diagnostics = acc_archive.archive_diagnostics(self.directory)
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("unfinished legacy checkpoint", diagnostics[0])
        self.assertIn(unfinished.name, diagnostics[0])

    def test_dry_run_does_not_create_archive_or_lock(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            rc = new_acc.main(
                ["--dir", str(self.directory), "--topic", "dry", "--dry-run"]
            )
        self.assertEqual(rc, 0)
        self.assertIn("_draft-001-", output.getvalue())
        self.assertFalse(self.directory.exists())

    def test_fresh_process_end_to_end_and_restart(self) -> None:
        create = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "scripts" / "new_acc.py"),
                "--dir",
                str(self.directory),
                "--topic",
                "journey",
                "--date",
                "2026-09-14",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        draft = Path(create.stdout.strip())
        _fill(draft, "fresh process journey")
        publish = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "scripts" / "finalize_acc.py"), str(draft)],
            text=True,
            capture_output=True,
            check=True,
        )
        final = Path(publish.stdout.strip())
        for _ in range(2):
            payload = json.dumps({"cwd": str(self.directory.parent.parent)})
            hook = subprocess.run(
                [sys.executable, "-B", str(REPO_ROOT / "scripts" / "acc_session_start.py")],
                input=payload,
                text=True,
                capture_output=True,
                check=True,
            )
            context = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn(final.name, context)
            self.assertIn("fresh process journey", context)
            self.assertNotIn("{{", context)

    def test_killed_finalizer_never_exposes_partial_final(self) -> None:
        self.directory.mkdir(parents=True)
        previous = self.directory / "001-2026-09-13-previous.md"
        previous.write_text(
            "# Session Checkpoint\n**Focus:** previous\nPrior completed context.\n",
            encoding="utf-8",
        )
        draft = _scaffold(self.directory, "interrupted")
        _fill(draft, "interrupted publication")
        marker = self.directory.parent / "publication-paused.marker"
        release = self.directory.parent / "publication-release.gate"
        worker = "\n".join(
            [
                "import inspect, pathlib, sys, time",
                "sys.path.insert(0, sys.argv[1])",
                "import finalize_acc",
                "source, first = inspect.getsourcelines(finalize_acc.publish_draft)",
                "target = first + next(i for i, line in enumerate(source) "
                "if 'handle.write(body)' in line)",
                "marker = pathlib.Path(sys.argv[3])",
                "release = pathlib.Path(sys.argv[4])",
                "def trace(frame, event, arg):",
                "    if (frame.f_code is finalize_acc.publish_draft.__code__ "
                "and event == 'line' and frame.f_lineno == target):",
                "        marker.touch()",
                "        while not release.exists():",
                "            time.sleep(0.01)",
                "    return trace",
                "sys.settrace(trace)",
                "finalize_acc.publish_draft(pathlib.Path(sys.argv[2]))",
            ]
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                worker,
                str(REPO_ROOT / "scripts"),
                str(draft),
                str(marker),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                break
            time.sleep(0.01)
        if not marker.exists():
            child.terminate()
            _, stderr = child.communicate(timeout=5)
            self.fail(stderr.decode() if stderr else "finalizer did not reach pause point")
        final = acc_archive.final_path_for_draft(draft)
        self.assertFalse(final.exists())
        self.assertEqual(find_latest_acc.find_latest(self.directory), previous)
        child.terminate()
        child.communicate(timeout=5)
        self.assertTrue(draft.exists())
        self.assertFalse(final.exists())
        self.assertEqual(find_latest_acc.find_latest(self.directory), previous)

        # The reserved draft can still be resumed and published after the crash.
        published = finalize_acc.publish_draft(draft)
        self.assertEqual(published, final)
        self.assertEqual(find_latest_acc.find_latest(self.directory), final)


class ConcurrentProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.directory = Path(self._tmp.name) / "archive"
        self.addCleanup(self._tmp.cleanup)

    def test_competing_processes_reserve_distinct_sequences_and_content(self) -> None:
        count = 12
        gate = Path(self._tmp.name) / "start.gate"
        worker = (
            "import pathlib,sys,time; "
            "sys.path.insert(0, sys.argv[1]); "
            "from new_acc import main; "
            "gate=pathlib.Path(sys.argv[2]); "
            "[(time.sleep(.005)) for _ in iter(int,1) if not gate.exists()]; "
            "raise SystemExit(main(['--dir',sys.argv[3],'--topic',sys.argv[4],"
            "'--focus',sys.argv[5],'--date','2026-09-14']))"
        )
        # Use a compact polling script rather than shell synchronization so the
        # test follows the same path on Windows and POSIX.
        worker = worker.replace(
            "[(time.sleep(.005)) for _ in iter(int,1) if not gate.exists()]",
            "exec(\"while not gate.exists():\\n time.sleep(.005)\")",
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    worker,
                    str(REPO_ROOT / "scripts"),
                    str(gate),
                    str(self.directory),
                    "same-topic",
                    f"focus-{index}",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for index in range(count)
        ]
        gate.touch()
        results = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
        self.assertTrue(all(result[2] == 0 for result in results), results)
        drafts = sorted(self.directory.glob("_draft-*.md"))
        self.assertEqual(len(drafts), count)
        sequences = [acc_archive.draft_parts(path)[0] for path in drafts]
        self.assertEqual(len(set(sequences)), count)
        combined = "\n".join(path.read_text(encoding="utf-8") for path in drafts)
        for index in range(count):
            self.assertIn(f"focus-{index}", combined)

        for index, draft in enumerate(drafts):
            _fill(draft, f"published-{index}")
        publishers = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(REPO_ROOT / "scripts" / "finalize_acc.py"),
                    str(draft),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for draft in drafts
        ]
        published = [
            process.communicate(timeout=20) + (process.returncode,) for process in publishers
        ]
        self.assertTrue(all(result[2] == 0 for result in published), published)
        finals = list(self.directory.glob("[0-9]*.md"))
        self.assertEqual(len(finals), count)
        self.assertEqual(list(self.directory.glob("_draft-*.md")), [])
        final_content = "\n".join(path.read_text(encoding="utf-8") for path in finals)
        for index in range(count):
            self.assertIn(f"published-{index}", final_content)

    def test_different_topic_producers_reserve_distinct_sequences(self) -> None:
        count = 8
        gate = Path(self._tmp.name) / "different-topics.gate"
        worker = "\n".join(
            [
                "import pathlib, sys, time",
                "sys.path.insert(0, sys.argv[1])",
                "from new_acc import main",
                "gate = pathlib.Path(sys.argv[2])",
                "while not gate.exists():",
                "    time.sleep(0.005)",
                "raise SystemExit(main(['--dir', sys.argv[3], '--topic', sys.argv[4], "
                "'--date', '2026-09-14']))",
            ]
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    worker,
                    str(REPO_ROOT / "scripts"),
                    str(gate),
                    str(self.directory),
                    f"topic-{index}",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for index in range(count)
        ]
        gate.touch()
        results = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
        self.assertTrue(all(result[2] == 0 for result in results), results)
        drafts = list(self.directory.glob("_draft-*.md"))
        sequences = [acc_archive.draft_parts(path)[0] for path in drafts]
        self.assertEqual(len(drafts), count)
        self.assertEqual(len(set(sequences)), count)
        for index in range(count):
            self.assertTrue(any(path.name.endswith(f"-topic-{index}.md") for path in drafts))

    def test_process_death_releases_archive_lock(self) -> None:
        self.directory.mkdir(parents=True)
        marker = Path(self._tmp.name) / "locked.marker"
        worker = (
            "import os,pathlib,sys; sys.path.insert(0,sys.argv[1]); "
            "from acc_archive import ArchiveLock; "
            "lock=ArchiveLock(pathlib.Path(sys.argv[2])); lock.__enter__(); "
            "pathlib.Path(sys.argv[3]).touch(); os._exit(0)"
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                worker,
                str(REPO_ROOT / "scripts"),
                str(self.directory),
                str(marker),
            ]
        )
        child.wait(timeout=10)
        self.assertTrue(marker.exists())
        with acc_archive.ArchiveLock(self.directory, timeout=1):
            pass

    def test_lock_timeout_creates_no_reservation(self) -> None:
        self.directory.mkdir(parents=True)
        marker = Path(self._tmp.name) / "locked.marker"
        release = Path(self._tmp.name) / "release.gate"
        worker = (
            "import pathlib,sys,time; sys.path.insert(0,sys.argv[1]); "
            "from acc_archive import ArchiveLock; "
            "archive=pathlib.Path(sys.argv[2]); marker=pathlib.Path(sys.argv[3]); "
            "release=pathlib.Path(sys.argv[4]); "
            "exec(\"with ArchiveLock(archive):\\n marker.touch()\\n "
            "while not release.exists():\\n  time.sleep(.01)\")"
        )
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                worker,
                str(REPO_ROOT / "scripts"),
                str(self.directory),
                str(marker),
                str(release),
            ]
        )
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        try:
            with self.assertRaises(TimeoutError):
                with acc_archive.ArchiveLock(self.directory, timeout=0.1, poll=0.01):
                    self.fail("second process should not acquire a held archive lock")
            self.assertEqual(list(self.directory.glob("_draft-*.md")), [])
            self.assertEqual(list(self.directory.glob("[0-9]*.md")), [])
        finally:
            release.touch()
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
