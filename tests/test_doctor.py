#!/usr/bin/env python3
"""Read-only archive diagnostics and command-line contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import acc_archive, acc_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "acc_doctor.py"
PRIVATE = "PRIVATE_CHECKPOINT_BODY_DO_NOT_DISCLOSE"
COMPLETE = (
    "# Session Checkpoint\n"
    f"**Focus:** {PRIVATE}\n"
    "## Decisions\n- D: Preserve the archive.\n"
    "## Current State\n- Work is ready.\n"
    "## Open Questions\n- Q: None.\n"
    "## Rejected Approaches\n- X: None.\n"
    "## Next Actions\n1. Continue.\n"
)


def _snapshot(directory: Path) -> dict:
    """Capture observable names, kinds and bytes without depending on atime."""
    result = {}
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory).as_posix()
        if path.is_symlink():
            result[name] = ("symlink", os.readlink(str(path)))
        elif path.is_dir():
            result[name] = ("directory",)
        else:
            result[name] = ("file", path.read_bytes())
    return result


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()

    def write(self, name: str, text: str = COMPLETE) -> Path:
        path = self.archive / name
        path.write_text(text, encoding="utf-8")
        return path

    def inspect(self) -> dict:
        before = _snapshot(self.root)
        report = acc_doctor.inspect_archive(self.archive)
        self.assertEqual(_snapshot(self.root), before)
        self.assertNotIn(PRIVATE, json.dumps(report))
        return report

    @staticmethod
    def codes(report: dict) -> set:
        return {finding["code"] for finding in report["findings"]}

    def run_cli(self, *args: str, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            cwd=str(self.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_complete_archive_has_stable_schema_and_no_findings(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        report = self.inspect()
        self.assertEqual(
            set(report),
            {
                "schema_version",
                "archive",
                "status",
                "latest",
                "selection_complete",
                "consistency",
                "counts",
                "entries",
                "findings",
                "omitted_findings",
            },
        )
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(Path(report["archive"]).resolve(), self.archive.resolve())
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["latest"], latest.name)
        self.assertTrue(report["selection_complete"])
        self.assertEqual(report["consistency"], "best_effort")
        self.assertEqual(
            report["counts"], {"finals": 1, "drafts": 0, "load_eligible": 1, "ignored": 0}
        )
        self.assertEqual(
            report["entries"],
            [{"name": latest.name, "kind": "final", "sequence": 1, "load_eligible": True}],
        )
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["omitted_findings"], 0)
        self.assertEqual(report, self.inspect())

    def test_empty_archive_is_healthy_and_does_not_create_a_lock(self) -> None:
        with mock.patch.object(acc_archive.ArchiveLock, "__enter__", side_effect=AssertionError):
            report = self.inspect()
        self.assertEqual(report["status"], "healthy")
        self.assertIsNone(report["latest"])
        self.assertEqual(
            report["counts"], {"finals": 0, "drafts": 0, "load_eligible": 0, "ignored": 0}
        )
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_missing_archive_returns_informational_report_without_creating_it(self) -> None:
        missing = self.root / "missing" / "docs" / "acc"
        before = _snapshot(self.root)
        report = acc_doctor.inspect_archive(missing)
        self.assertEqual(report["status"], "missing")
        self.assertIsNone(report["latest"])
        self.assertIn("archive_missing", self.codes(report))
        self.assertEqual(_snapshot(self.root), before)
        result = self.run_cli("--dir", str(missing), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "missing")
        self.assertEqual(_snapshot(self.root), before)

    def test_file_in_place_of_archive_is_error_and_preserved(self) -> None:
        path = self.root / "not-a-directory"
        path.write_bytes(b"private archive placeholder")
        before = _snapshot(self.root)
        report = acc_doctor.inspect_archive(path)
        self.assertEqual(report["status"], "error")
        self.assertIn("archive_not_directory", self.codes(report))
        result = self.run_cli("--dir", str(path), "--json")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "error")
        self.assertEqual(_snapshot(self.root), before)

    def test_latest_sequence_is_numeric_across_999_to_1000(self) -> None:
        self.write("999-2026-09-17-earlier.md")
        latest = self.write("1000-2026-09-16-later.md")
        report = self.inspect()
        self.assertEqual(report["latest"], latest.name)
        self.assertEqual(report["latest"], acc_archive.find_latest_completed(self.archive).name)
        self.assertEqual([entry["sequence"] for entry in report["entries"]], [999, 1000])

    def test_completed_duplicate_ids_fall_back_to_unique_checkpoint(self) -> None:
        fallback = self.write("009-2026-09-17-fallback.md")
        self.write("010-2026-09-17-alpha.md")
        self.write("0010-2026-09-16-beta.md")
        report = self.inspect()
        self.assertEqual(report["status"], "findings")
        self.assertIn("duplicate_sequence", self.codes(report))
        self.assertEqual(report["latest"], fallback.name)
        self.assertEqual(report["latest"], acc_archive.find_latest_completed(self.archive).name)
        self.assertEqual(report["counts"]["load_eligible"], 1)
        duplicate_entries = [entry for entry in report["entries"] if entry["sequence"] == 10]
        self.assertTrue(all(not entry["load_eligible"] for entry in duplicate_entries))

    def test_only_duplicate_completed_ids_have_no_authoritative_latest(self) -> None:
        self.write("010-2026-09-17-alpha.md")
        self.write("010-2026-09-17-beta.md")
        report = self.inspect()
        self.assertIsNone(report["latest"])
        self.assertTrue(report["selection_complete"])
        self.assertIn("duplicate_sequence", self.codes(report))

    def test_unfinished_final_does_not_make_complete_peer_a_duplicate(self) -> None:
        latest = self.write("010-2026-09-17-complete.md")
        self.write("010-2026-09-17-unfinished.md", COMPLETE + "{{TOKENS_BEFORE}}\n")
        report = self.inspect()
        self.assertEqual(report["latest"], latest.name)
        self.assertIn("unfinished_final", self.codes(report))
        self.assertNotIn("duplicate_sequence", self.codes(report))

    def test_drafts_are_informational_and_never_selectable(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        draft = self.write("_draft-002-2026-09-17-pending.md", "{{FOCUS}}\n" + PRIVATE)
        original_open = acc_doctor._open_nofollow

        def guarded_open(path):
            if path == draft.resolve():
                raise AssertionError("doctor attempted to read a draft body")
            return original_open(path)

        with mock.patch.object(acc_doctor, "_open_nofollow", guarded_open):
            report = self.inspect()
        self.assertEqual(report["latest"], latest.name)
        drafts = [entry for entry in report["entries"] if entry["kind"] == "draft"]
        self.assertEqual(len(drafts), 1)
        self.assertFalse(drafts[0]["load_eligible"])
        findings = [finding for finding in report["findings"] if finding["code"] == "draft_present"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "info")
        result = self.run_cli("--dir", str(self.archive), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_structure_warning_does_not_override_loader_eligibility(self) -> None:
        self.write("001-2026-09-17-complete.md")
        legacy = self.write("002-2026-09-17-legacy.md", f"Legacy checkpoint body {PRIVATE}\n")
        report = self.inspect()
        self.assertIn("legacy_structure", self.codes(report))
        self.assertEqual(report["latest"], legacy.name)
        self.assertEqual(report["latest"], acc_archive.find_latest_completed(self.archive).name)
        entry = next(item for item in report["entries"] if item["name"] == legacy.name)
        self.assertTrue(entry["load_eligible"])

    def test_unfinished_tokens_and_empty_sections_are_excluded(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        self.write("002-2026-09-17-token.md", COMPLETE + "{{TOKENS_AFTER}}\n")
        self.write(
            "003-2026-09-17-empty.md",
            COMPLETE.replace("- Q: None.", "- Q:"),
        )
        report = self.inspect()
        self.assertEqual(report["latest"], latest.name)
        unfinished = [
            finding for finding in report["findings"] if finding["code"] == "unfinished_final"
        ]
        self.assertEqual(len(unfinished), 2)
        self.assertEqual(report["latest"], acc_archive.find_latest_completed(self.archive).name)

    def test_line_endings_preserve_complete_and_unfinished_loader_semantics(self) -> None:
        for label, separators in (
            ("crlf", ("\r\n",)),
            ("cr", ("\r",)),
            ("mixed", ("\r\n", "\r", "\n")),
        ):
            for unfinished in (False, True):
                with self.subTest(line_endings=label, unfinished=unfinished):
                    directory = self.archive / f"{label}-{unfinished}"
                    directory.mkdir()
                    older = directory / "001-2026-09-17-stable.md"
                    older.write_bytes(COMPLETE.encode("utf-8"))
                    newer = directory / "002-2026-09-17-variant.md"
                    content = COMPLETE.replace("- Q: None.", "- Q:") if unfinished else COMPLETE
                    encoded = "".join(
                        line + separators[index % len(separators)]
                        for index, line in enumerate(content.splitlines())
                    ).encode("utf-8")
                    newer.write_bytes(encoded)
                    before = _snapshot(self.root)
                    report = acc_doctor.inspect_archive(directory)
                    shared_latest = acc_archive.find_latest_completed(directory)
                    self.assertEqual(report["latest"], shared_latest.name)
                    self.assertEqual(report["latest"], older.name if unfinished else newer.name)
                    newer_entry = next(
                        entry for entry in report["entries"] if entry["name"] == newer.name
                    )
                    self.assertEqual(newer_entry["load_eligible"], not unfinished)
                    self.assertEqual("unfinished_final" in self.codes(report), unfinished)
                    self.assertEqual(report["status"], "findings" if unfinished else "healthy")
                    self.assertEqual(_snapshot(self.root), before)
                    self.assertNotIn(PRIVATE, json.dumps(report))

    def test_nonentry_files_and_named_directories_are_not_read_or_loaded(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        self.write("README.md", PRIVATE)
        self.write("_ledger.md", PRIVATE)
        self.write(".acc.lock", "existing lock bytes")
        directory = self.archive / "999-2026-09-17-directory.md"
        directory.mkdir()
        (directory / "nested.md").write_text(PRIVATE, encoding="utf-8")
        original_open = acc_doctor._open_nofollow

        def guarded_open(path):
            if path != latest.resolve():
                raise AssertionError("doctor attempted to read a nonentry")
            return original_open(path)

        with mock.patch.object(acc_doctor, "_open_nofollow", guarded_open):
            report = self.inspect()
        self.assertEqual(report["latest"], latest.name)
        self.assertIn("entry_not_file", self.codes(report))
        self.assertTrue(all(item["name"] != "README.md" for item in report["entries"]))
        self.assertTrue(all(item["name"] != "_ledger.md" for item in report["entries"]))

    def test_symlinks_are_diagnosed_without_reading_their_targets(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        target = self.root / "external.md"
        target.write_text(PRIVATE, encoding="utf-8")
        linked = self.archive / "999-2026-09-17-linked.md"
        broken = self.archive / "1000-2026-09-17-broken.md"
        try:
            linked.symlink_to(target)
            broken.symlink_to(self.root / "absent.md")
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        original_open = acc_doctor._open_nofollow
        forbidden = {path.parent.resolve() / path.name for path in (linked, broken, target)}

        def guarded_open(path):
            if path in forbidden:
                raise AssertionError("doctor attempted to open a symlink target")
            return original_open(path)

        before = _snapshot(self.root)
        with mock.patch.object(acc_doctor, "_open_nofollow", guarded_open):
            report = acc_doctor.inspect_archive(self.archive)
        self.assertEqual(_snapshot(self.root), before)
        self.assertEqual(report["latest"], latest.name)
        links = [finding for finding in report["findings"] if finding["code"] == "entry_symlink"]
        self.assertEqual({finding["path"] for finding in links}, {linked.name, broken.name})
        self.assertNotIn(PRIVATE, json.dumps(report))

    def test_invalid_utf8_is_reported_without_leaking_decoded_prefix(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        broken = self.archive / "002-2026-09-17-invalid.md"
        broken.write_bytes(PRIVATE.encode("ascii") + b"\xff\xfe")
        report = self.inspect()
        self.assertEqual(report["latest"], latest.name)
        self.assertIn("entry_invalid_utf8", self.codes(report))
        self.assertEqual(report["latest"], acc_archive.find_latest_completed(self.archive).name)

    def test_entry_read_error_is_content_free_and_other_entries_remain_reported(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        unreadable = self.write("002-2026-09-17-unreadable.md")
        original_open = acc_doctor._open_nofollow
        canonical_unreadable = unreadable.resolve()

        def failing_open(path):
            if path == canonical_unreadable:
                raise PermissionError(PRIVATE)
            return original_open(path)

        before = _snapshot(self.root)
        with mock.patch.object(acc_doctor, "_open_nofollow", failing_open):
            report = acc_doctor.inspect_archive(self.archive)
        self.assertEqual(_snapshot(self.root), before)
        self.assertEqual(report["latest"], latest.name)
        self.assertIn("entry_unreadable", self.codes(report))
        self.assertNotIn(PRIVATE, json.dumps(report))

    def test_archive_listing_error_is_content_free_inspection_error(self) -> None:
        before = _snapshot(self.root)
        with mock.patch.object(acc_doctor.os, "scandir", side_effect=PermissionError(PRIVATE)):
            report = acc_doctor.inspect_archive(self.archive)
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["selection_complete"])
        self.assertIsNone(report["latest"])
        self.assertIn("archive_unreadable", self.codes(report))
        self.assertNotIn(PRIVATE, json.dumps(report))
        self.assertEqual(_snapshot(self.root), before)

    def test_entry_size_limit_suppresses_selection_instead_of_hiding_newer_file(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        byte_count = len(latest.read_bytes())
        with mock.patch.object(acc_doctor, "MAX_ENTRY_BYTES", byte_count):
            self.assertEqual(self.inspect()["latest"], latest.name)
        with mock.patch.object(acc_doctor, "MAX_ENTRY_BYTES", byte_count - 1):
            report = self.inspect()
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["selection_complete"])
        self.assertIsNone(report["latest"])
        self.assertIn("entry_limit_exceeded", self.codes(report))
        self.assertIsNone(report["entries"][0]["load_eligible"])

    def test_directory_limit_returns_incomplete_without_claiming_partial_latest(self) -> None:
        self.write("001-2026-09-17-first.md")
        self.write("002-2026-09-17-second.md")
        with mock.patch.object(acc_doctor, "MAX_ENTRIES", 1):
            report = self.inspect()
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["selection_complete"])
        self.assertIsNone(report["latest"])
        self.assertIn("archive_limit_exceeded", self.codes(report))

    def test_total_read_budget_suppresses_latest_without_opening_uninspected_entries(self) -> None:
        first = self.write("001-2026-09-17-first.md")
        second = self.write("002-2026-09-17-second.md")
        original_open = acc_doctor._open_nofollow
        opened = []

        def recording_open(path):
            opened.append(path)
            return original_open(path)

        with mock.patch.object(acc_doctor, "MAX_TOTAL_READ_BYTES", len(first.read_bytes())):
            with mock.patch.object(acc_doctor, "_open_nofollow", recording_open):
                report = self.inspect()
        self.assertEqual(opened, [first.resolve()])
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["selection_complete"])
        self.assertIsNone(report["latest"])
        self.assertIn("archive_read_limit_exceeded", self.codes(report))
        uninspected = next(entry for entry in report["entries"] if entry["name"] == second.name)
        self.assertIsNone(uninspected["load_eligible"])

    def test_finding_cap_does_not_hide_warning_status_or_stop_entry_inspection(self) -> None:
        for sequence in range(1, 4):
            self.write(f"_draft-{sequence:03d}-2026-09-17-pending.md", PRIVATE)
        invalid = self.archive / "004-2026-09-17-invalid.md"
        invalid.write_bytes(b"\xff")
        with mock.patch.object(acc_doctor, "MAX_FINDINGS", 2):
            report = self.inspect()
        self.assertEqual(len(report["findings"]), 2)
        self.assertTrue(all(item["severity"] == "info" for item in report["findings"]))
        self.assertEqual(report["omitted_findings"], 2)
        self.assertEqual(report["status"], "findings")
        self.assertEqual(report["counts"]["finals"], 1)
        self.assertEqual(report["counts"]["drafts"], 3)
        self.assertTrue(report["selection_complete"])

    def test_entry_changed_between_metadata_and_read_suppresses_latest(self) -> None:
        self.write("001-2026-09-17-stable.md")
        changed = self.write("002-2026-09-17-changing.md")
        original_open = acc_doctor._open_nofollow
        changed_content = COMPLETE + "\nConcurrent writer changed the checkpoint.\n"
        canonical_changed = changed.resolve()

        def concurrent_open(path):
            if path == canonical_changed:
                changed.write_text(changed_content, encoding="utf-8")
            return original_open(path)

        before = _snapshot(self.root)
        with mock.patch.object(acc_doctor, "_open_nofollow", concurrent_open):
            report = acc_doctor.inspect_archive(self.archive)
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["selection_complete"])
        self.assertIsNone(report["latest"])
        self.assertIn("archive_changed", self.codes(report))
        before[changed.relative_to(self.root).as_posix()] = ("file", changed.read_bytes())
        self.assertEqual(_snapshot(self.root), before)
        self.assertEqual(changed.read_text(encoding="utf-8"), changed_content)
        self.assertNotIn(PRIVATE, json.dumps(report))

    def test_symlink_substitution_between_metadata_and_open_does_not_open_target(self) -> None:
        self.write("001-2026-09-17-stable.md")
        changed = self.write("999-2026-09-17-changing.md")
        target = self.root / "external-private.md"
        target.write_text(PRIVATE, encoding="utf-8")
        probe = self.root / "symlink-probe"
        try:
            probe.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        probe.unlink()
        canonical_changed = changed.resolve()
        target_details = target.stat()
        original_open = acc_doctor._open_nofollow
        original_fdopen = os.fdopen

        def replacing_open(path):
            if path == canonical_changed:
                path.unlink()
                path.symlink_to(target.resolve())
            return original_open(path)

        def guarded_fdopen(descriptor, *args, **kwargs):
            details = os.fstat(descriptor)
            if (details.st_dev, details.st_ino) == (target_details.st_dev, target_details.st_ino):
                os.close(descriptor)
                raise AssertionError("doctor followed substituted symlink to external content")
            return original_fdopen(descriptor, *args, **kwargs)

        before = _snapshot(self.root)
        with mock.patch.object(acc_doctor, "_open_nofollow", replacing_open):
            with mock.patch.object(acc_doctor.os, "fdopen", guarded_fdopen):
                report = acc_doctor.inspect_archive(self.archive)
        self.assertTrue(changed.is_symlink())
        self.assertNotEqual(report["latest"], changed.name)
        replaced_entry = next(entry for entry in report["entries"] if entry["name"] == changed.name)
        self.assertFalse(replaced_entry["load_eligible"])
        self.assertTrue({"archive_changed", "entry_unreadable"} & self.codes(report))
        before[changed.relative_to(self.root).as_posix()] = ("symlink", os.readlink(str(changed)))
        self.assertEqual(_snapshot(self.root), before)
        self.assertNotIn(PRIVATE, json.dumps(report))

    def test_global_default_home_is_used_without_environment_override(self) -> None:
        target = self.root / "isolated-home" / ".claude" / "acc"
        target.mkdir(parents=True)
        latest = target / "005-2026-09-17-global-default.md"
        latest.write_text(COMPLETE, encoding="utf-8")
        before = _snapshot(self.root)
        output = StringIO()
        with mock.patch.dict(os.environ, {"ACC_GLOBAL_DIR": ""}):
            with mock.patch.object(Path, "home", return_value=target.parent.parent):
                with redirect_stdout(output):
                    result = acc_doctor.main(["--global", "--json"])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(Path(report["archive"]).resolve(), target.resolve())
        self.assertEqual(report["latest"], latest.name)
        self.assertEqual(_snapshot(self.root), before)

    def test_cli_json_is_deterministic_and_does_not_change_archive(self) -> None:
        self.write("001-2026-09-17-complete.md")
        before = _snapshot(self.root)
        first = self.run_cli("--dir", str(self.archive), "--json")
        second = self.run_cli("--dir", str(self.archive), "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout), acc_doctor.inspect_archive(self.archive))
        self.assertNotIn(PRIVATE, first.stdout + first.stderr)
        self.assertEqual(_snapshot(self.root), before)

    def test_plain_python_does_not_write_bytecode_when_bundle_is_inside_archive(self) -> None:
        latest = self.write("001-2026-09-17-complete.md")
        for name in ("acc_doctor.py", "acc_archive.py"):
            (self.archive / name).write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)
        before = _snapshot(self.root)
        result = subprocess.run(
            [
                sys.executable,
                str(self.archive / "acc_doctor.py"),
                "--dir",
                str(self.archive),
                "--json",
            ],
            cwd=str(self.root),
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["latest"], latest.name)
        self.assertEqual(_snapshot(self.root), before)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)

    def test_cli_default_uses_docs_acc_beneath_working_directory(self) -> None:
        target = self.root / "docs" / "acc"
        target.mkdir(parents=True)
        latest = target / "005-2026-09-17-default.md"
        latest.write_text(COMPLETE, encoding="utf-8")
        before = _snapshot(self.root)
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(Path(report["archive"]).resolve(), target.resolve())
        self.assertEqual(report["latest"], latest.name)
        self.assertEqual(_snapshot(self.root), before)

    def test_cli_global_uses_explicit_environment_archive(self) -> None:
        latest = self.write("005-2026-09-17-global.md")
        environment = dict(os.environ, ACC_GLOBAL_DIR=str(self.archive))
        before = _snapshot(self.root)
        result = self.run_cli("--global", "--json", env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(Path(report["archive"]).resolve(), self.archive.resolve())
        self.assertEqual(report["latest"], latest.name)
        self.assertEqual(_snapshot(self.root), before)

    def test_cli_archive_options_are_mutually_exclusive(self) -> None:
        before = _snapshot(self.root)
        result = self.run_cli("--global", "--dir", str(self.archive), "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("not allowed", result.stderr)
        self.assertEqual(_snapshot(self.root), before)

    def test_cli_help_succeeds_without_creating_the_default_archive(self) -> None:
        before = _snapshot(self.root)
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        for option in ("--dir", "--global", "--json"):
            self.assertIn(option, result.stdout)
        self.assertEqual(_snapshot(self.root), before)

    def test_cli_warnings_exit_one_and_text_explains_latest_without_body(self) -> None:
        latest = self.write("001-2026-09-17-legacy.md", PRIVATE)
        before = _snapshot(self.root)
        result = self.run_cli("--dir", str(self.archive))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(latest.name, result.stdout)
        self.assertIn("legacy", result.stdout.lower())
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        self.assertEqual(_snapshot(self.root), before)

    def test_main_json_inspection_error_returns_two_without_traceback(self) -> None:
        output, errors = StringIO(), StringIO()
        with mock.patch.object(acc_doctor.os, "scandir", side_effect=PermissionError(PRIVATE)):
            with redirect_stdout(output), redirect_stderr(errors):
                result = acc_doctor.main(["--dir", str(self.archive), "--json"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")
        self.assertNotIn(PRIVATE, output.getvalue() + errors.getvalue())
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()
