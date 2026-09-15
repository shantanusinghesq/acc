#!/usr/bin/env python3
"""Tests for the deterministic ACC helper scripts.

Stdlib-only (unittest), so they run anywhere Python does without `pip`:

    python -m unittest discover -s tests       # from the repo root
    python -m pytest tests                      # if pytest is installed

The scripts encode the two pieces of logic the skill must get right every
time: which file is the *latest* ACC (Mode B) and what the *next* entry is
named (Mode A). These tests pin that behavior so a refactor can't silently
break selection or numbering.
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
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


find_latest_acc = _load("find_latest_acc")
new_acc = _load("new_acc")
finalize_acc = _load("finalize_acc")


def _touch(directory: Path, name: str) -> Path:
    p = directory / name
    p.write_text("stub\n", encoding="utf-8")
    return p


def _fill_and_publish(draft: Path) -> Path:
    body = draft.read_text(encoding="utf-8")
    body = body.replace("{{TOKENS_BEFORE}}", "10").replace("{{TOKENS_AFTER}}", "1")
    body = body.replace("- D: \n", "- D: Preserve the tested contract.\n")
    body = body.replace("## Current State\n- \n", "## Current State\n- Test fixture ready.\n")
    body = body.replace("- Q: \n", "- Q: None.\n")
    body = body.replace("- X: \n", "- X: None.\n")
    body = body.replace("## Next Actions\n1. \n", "## Next Actions\n1. Continue.\n")
    draft.write_text(body, encoding="utf-8")
    return finalize_acc.publish_draft(draft)


class FindLatestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_dir_returns_none(self) -> None:
        self.assertIsNone(find_latest_acc.find_latest(self.dir / "nope"))

    def test_empty_dir_returns_none(self) -> None:
        self.assertIsNone(find_latest_acc.find_latest(self.dir))

    def test_only_readme_returns_none(self) -> None:
        _touch(self.dir, "README.md")
        self.assertIsNone(find_latest_acc.find_latest(self.dir))

    def test_picks_highest_numeric_sequence(self) -> None:
        _touch(self.dir, "001-2026-01-01-alpha.md")
        _touch(self.dir, "002-2026-02-01-beta.md")
        latest = _touch(self.dir, "010-2026-03-01-gamma.md")
        self.assertEqual(find_latest_acc.find_latest(self.dir), latest)

    def test_numeric_order_handles_ten_and_hundred(self) -> None:
        _touch(self.dir, "009-2026-01-01-a.md")
        _touch(self.dir, "099-2026-01-01-b.md")
        latest = _touch(self.dir, "100-2026-01-01-c.md")
        self.assertEqual(find_latest_acc.find_latest(self.dir), latest)

    def test_readme_excluded_even_though_it_sorts_after_digits(self) -> None:
        # 'R' (0x52) sorts after digits (0x30-0x39); without the exclusion
        # README.md would be wrongly chosen as the newest entry.
        latest = _touch(self.dir, "001-2026-01-01-alpha.md")
        _touch(self.dir, "README.md")
        self.assertEqual(find_latest_acc.find_latest(self.dir), latest)

    def test_readme_exclusion_is_case_insensitive(self) -> None:
        latest = _touch(self.dir, "001-2026-01-01-alpha.md")
        _touch(self.dir, "readme.md")
        self.assertEqual(find_latest_acc.find_latest(self.dir), latest)

    def test_extractor_outputs_excluded_even_though_they_sort_after_digits(self) -> None:
        # '_' (0x5F) sorts after digits (0x30-0x39); without the exclusion the
        # /acc-extract outputs (_ledger.md, _backlog.md, _avoid.md) would be
        # wrongly chosen over the newest real entry.
        latest = _touch(self.dir, "007-2026-06-11-fixes.md")
        _touch(self.dir, "_ledger.md")
        _touch(self.dir, "_backlog.md")
        _touch(self.dir, "_avoid.md")
        self.assertEqual(find_latest_acc.find_latest(self.dir), latest)

    def test_only_extractor_outputs_returns_none(self) -> None:
        _touch(self.dir, "_ledger.md")
        self.assertIsNone(find_latest_acc.find_latest(self.dir))

    def test_main_missing_dir_exit_1(self) -> None:
        self.assertEqual(find_latest_acc.main(["--dir", str(self.dir / "nope")]), 1)

    def test_main_empty_dir_exit_1(self) -> None:
        self.assertEqual(find_latest_acc.main(["--dir", str(self.dir)]), 1)

    def test_main_success_exit_0_and_prints_path(self) -> None:
        _touch(self.dir, "001-2026-01-01-alpha.md")
        buf = StringIO()
        with redirect_stdout(buf):
            rc = find_latest_acc.main(["--dir", str(self.dir)])
        self.assertEqual(rc, 0)
        self.assertIn("001-2026-01-01-alpha.md", buf.getvalue().strip())


class SlugifyTests(unittest.TestCase):
    def test_basic_lowercase_and_hyphens(self) -> None:
        self.assertEqual(new_acc.slugify("Auth Rewrite"), "auth-rewrite")

    def test_collapses_runs_and_strips_edges(self) -> None:
        self.assertEqual(new_acc.slugify("  --Foo / Bar!!  "), "foo-bar")

    def test_keeps_digits(self) -> None:
        self.assertEqual(new_acc.slugify("v2 API migration"), "v2-api-migration")

    def test_empty_falls_back_to_session(self) -> None:
        self.assertEqual(new_acc.slugify("!!!"), "session")
        self.assertEqual(new_acc.slugify(""), "session")


class NextSeqTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_empty_starts_at_one(self) -> None:
        self.assertEqual(new_acc.next_seq(self.dir), 1)

    def test_missing_dir_starts_at_one(self) -> None:
        self.assertEqual(new_acc.next_seq(self.dir / "nope"), 1)

    def test_increments_past_highest(self) -> None:
        _touch(self.dir, "001-2026-01-01-a.md")
        _touch(self.dir, "007-2026-01-01-b.md")
        self.assertEqual(new_acc.next_seq(self.dir), 8)

    def test_ignores_non_sequence_names(self) -> None:
        _touch(self.dir, "README.md")
        _touch(self.dir, "notes.md")
        _touch(self.dir, "_ledger.md")
        _touch(self.dir, "003-2026-01-01-c.md")
        self.assertEqual(new_acc.next_seq(self.dir), 4)

    def test_handles_four_digit_sequences(self) -> None:
        _touch(self.dir, "1000-2026-01-01-a.md")
        self.assertEqual(new_acc.next_seq(self.dir), 1001)


class NewAccMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "docs" / "acc"
        self.addCleanup(self._tmp.cleanup)

    def _run(self, *args: str) -> int:
        return new_acc.main(["--dir", str(self.dir), *args])

    def test_creates_excluded_draft_with_padded_name(self) -> None:
        rc = self._run("--topic", "Auth Rewrite", "--date", "2026-05-27")
        self.assertEqual(rc, 0)
        out = self.dir / "_draft-001-2026-05-27-auth-rewrite.md"
        self.assertTrue(out.is_file())

    def test_seeds_readme_on_first_run(self) -> None:
        self._run("--topic", "alpha", "--date", "2026-01-01")
        self.assertTrue((self.dir / "README.md").is_file())

    def test_substitutes_date_and_focus(self) -> None:
        self._run("--topic", "alpha", "--focus", "the auth layer", "--date", "2026-01-01")
        body = (self.dir / "_draft-001-2026-01-01-alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("{{DATE}}", body)
        self.assertNotIn("{{FOCUS}}", body)
        self.assertIn("2026-01-01", body)
        self.assertIn("the auth layer", body)

    def test_focus_defaults_to_topic(self) -> None:
        self._run("--topic", "auth-rewrite", "--date", "2026-01-01")
        body = (self.dir / "_draft-001-2026-01-01-auth-rewrite.md").read_text(encoding="utf-8")
        self.assertIn("auth-rewrite", body)

    def test_second_run_increments_sequence(self) -> None:
        self._run("--topic", "alpha", "--date", "2026-01-01")
        self._run("--topic", "beta", "--date", "2026-01-02")
        self.assertTrue((self.dir / "_draft-002-2026-01-02-beta.md").is_file())

    def test_invalid_date_exit_2(self) -> None:
        self.assertEqual(self._run("--topic", "alpha", "--date", "not-a-date"), 2)

    def test_dry_run_prints_path_without_writing(self) -> None:
        buf = StringIO()
        with redirect_stdout(buf):
            rc = self._run("--topic", "alpha", "--date", "2026-01-01", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("_draft-001-2026-01-01-alpha.md", buf.getvalue())
        # Nothing should have been created on disk.
        self.assertFalse((self.dir / "_draft-001-2026-01-01-alpha.md").exists())
        self.assertFalse((self.dir / "README.md").exists())
        self.assertFalse(self.dir.exists())

    def test_dry_run_reports_next_seq(self) -> None:
        self._run("--topic", "alpha", "--date", "2026-01-01")
        buf = StringIO()
        with redirect_stdout(buf):
            rc = self._run("--topic", "beta", "--date", "2026-01-02", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("_draft-002-2026-01-02-beta.md", buf.getvalue())
        self.assertFalse((self.dir / "_draft-002-2026-01-02-beta.md").exists())

    def test_dir_and_global_are_mutually_exclusive(self) -> None:
        err = StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(err):
            new_acc.main(["--topic", "a", "--dir", str(self.dir), "--global"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not allowed with", err.getvalue())

    def test_refuses_overwrite_exit_3(self) -> None:
        # The guard is defensive: under normal flow next_seq is always fresh,
        # so a collision only happens if numbering is forced backwards. Pin
        # next_seq to 1 with 001 already on disk to exercise the guard.
        self.dir.mkdir(parents=True, exist_ok=True)
        _touch(self.dir, "_draft-001-2026-01-01-alpha.md")
        original = new_acc.next_seq
        new_acc.next_seq = lambda _dir: 1
        self.addCleanup(setattr, new_acc, "next_seq", original)
        self.assertEqual(self._run("--topic", "alpha", "--date", "2026-01-01"), 3)


class GlobalArchiveTests(unittest.TestCase):
    """--global routes the scripts at the cross-project archive."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.global_dir = Path(self._tmp.name) / "global-acc"
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"ACC_GLOBAL_DIR": str(self.global_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_env_var_overrides_location(self) -> None:
        self.assertEqual(new_acc.global_dir(), self.global_dir)
        self.assertEqual(find_latest_acc.global_dir(), self.global_dir)

    def test_default_location_is_under_home(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("ACC_GLOBAL_DIR", None)
            self.assertEqual(new_acc.global_dir(), Path.home() / ".claude" / "acc")

    def test_global_dir_agrees_across_all_four_scripts(self) -> None:
        # global_dir() is duplicated per script (standalone-script design); pin
        # the four copies together so they cannot drift — the find_latest()
        # duplication already required a two-site fix once (PR #4).
        modules = [new_acc, find_latest_acc, _load("list_acc"), _load("acc_session_start")]
        for module in modules:
            with self.subTest(module=module.__name__, env="set"):
                self.assertEqual(module.global_dir(), self.global_dir)
        with mock.patch.dict(os.environ):
            os.environ.pop("ACC_GLOBAL_DIR", None)
            expected = Path.home() / ".claude" / "acc"
            for module in modules:
                with self.subTest(module=module.__name__, env="unset"):
                    self.assertEqual(module.global_dir(), expected)

    def test_dir_and_global_mutually_exclusive_in_readers(self) -> None:
        # new_acc and acc_session_start pin this elsewhere; cover the two
        # remaining copies of the copy-pasted flag wiring.
        for module in (find_latest_acc, _load("list_acc")):
            with self.subTest(module=module.__name__):
                err = StringIO()
                with self.assertRaises(SystemExit) as ctx, redirect_stderr(err):
                    module.main(["--dir", str(self.global_dir), "--global"])
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("not allowed with", err.getvalue())

    def test_new_acc_global_writes_to_global_archive(self) -> None:
        buf = StringIO()
        with redirect_stdout(buf):
            rc = new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--global"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.global_dir / "_draft-001-2026-01-01-alpha.md").is_file())

    def test_find_latest_global_reads_global_archive(self) -> None:
        with redirect_stdout(StringIO()):
            new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--global"])
        _fill_and_publish(self.global_dir / "_draft-001-2026-01-01-alpha.md")
        buf = StringIO()
        with redirect_stdout(buf):
            rc = find_latest_acc.main(["--global"])
        self.assertEqual(rc, 0)
        self.assertIn("001-2026-01-01-alpha.md", buf.getvalue())

    def test_global_and_project_archives_are_independent(self) -> None:
        # An entry produced globally must not affect project-dir numbering.
        with redirect_stdout(StringIO()):
            new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--global"])
            project = Path(self._tmp.name) / "docs" / "acc"
            new_acc.main(["--topic", "beta", "--date", "2026-01-02", "--dir", str(project)])
        self.assertTrue((project / "_draft-001-2026-01-02-beta.md").is_file())

    def test_global_entry_carries_source_stamp(self) -> None:
        # Issue #6: global entries record the producing project so the
        # SessionStart hook can announce where a checkpoint came from.
        with redirect_stdout(StringIO()):
            rc = new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--global"])
        self.assertEqual(rc, 0)
        body = (self.global_dir / "_draft-001-2026-01-01-alpha.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"**Source project:** {Path.cwd()}", body)

    def test_source_stamp_lands_after_focus_line(self) -> None:
        with redirect_stdout(StringIO()):
            new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--global"])
        text = (self.global_dir / "_draft-001-2026-01-01-alpha.md").read_text(
            encoding="utf-8"
        )
        lines = text.splitlines()
        focus_at = next(i for i, line in enumerate(lines) if line.startswith("**Focus:**"))
        self.assertTrue(lines[focus_at + 1].startswith("**Source project:**"))

    def test_project_entry_has_no_source_stamp(self) -> None:
        project = Path(self._tmp.name) / "docs" / "acc"
        with redirect_stdout(StringIO()):
            new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--dir", str(project)])
        body = (project / "_draft-001-2026-01-01-alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("**Source project:**", body)

    def test_global_archive_gets_global_readme_seed(self) -> None:
        # Issue #6 nit: the global archive must not be seeded with the
        # project-oriented README (H1 "ACC Archive — `docs/acc/`").
        with redirect_stdout(StringIO()):
            new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--global"])
        readme = (self.global_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("ACC Global Archive", readme)
        self.assertIn("Source project", readme)

    def test_project_archive_keeps_project_readme_seed(self) -> None:
        project = Path(self._tmp.name) / "docs" / "acc"
        with redirect_stdout(StringIO()):
            new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--dir", str(project)])
        readme = (project / "README.md").read_text(encoding="utf-8")
        self.assertIn("ACC Archive — `docs/acc/`", readme)

    def test_stale_project_seed_in_global_archive_is_replaced(self) -> None:
        # A global archive created before the global seed existed carries the
        # project-oriented README verbatim; --global re-seeds it.
        self.global_dir.mkdir(parents=True)
        project_seed = new_acc.README_SEED.read_text(encoding="utf-8")
        (self.global_dir / "README.md").write_text(project_seed, encoding="utf-8")
        with redirect_stdout(StringIO()):
            new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--global"])
        readme = (self.global_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn("ACC Global Archive", readme)

    def test_user_modified_global_readme_is_left_alone(self) -> None:
        self.global_dir.mkdir(parents=True)
        custom = "# My own notes about this archive\n"
        (self.global_dir / "README.md").write_text(custom, encoding="utf-8")
        with redirect_stdout(StringIO()):
            new_acc.main(["--topic", "alpha", "--date", "2026-01-01", "--global"])
        readme = (self.global_dir / "README.md").read_text(encoding="utf-8")
        self.assertEqual(readme, custom)


if __name__ == "__main__":
    unittest.main()
