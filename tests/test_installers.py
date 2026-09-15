#!/usr/bin/env python3
"""Isolated regression tests for the PowerShell and POSIX installers.

Every installer invocation uses a temporary source checkout and skills
directory. These tests never read or modify a real Claude installation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent


class InstallerFixture:
    script_name = ""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_source(self, path: Path) -> Path:
        path.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / self.script_name, path / self.script_name)
        (path / "SKILL.md").write_text("---\nname: acc\n---\n", encoding="utf-8")
        (path / "source-sentinel.txt").write_text("preserve me", encoding="utf-8")
        return path

    def run_installer(self, source: Path, skills_dir: Path, *, copy: bool = True):
        raise NotImplementedError

    def assert_overlap_refused(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(
            result.stdout + result.stderr,
            r"source\s+and\s+destination overlap",
        )

    def test_copy_install_and_reinstall(self) -> None:
        source = self.make_source(self.root / "checkout")
        skills = self.root / "claude-skills"

        first = self.run_installer(source, skills)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        destination = skills / "acc"
        self.assertEqual(
            (destination / "source-sentinel.txt").read_text(encoding="utf-8"),
            "preserve me",
        )

        (destination / "obsolete.txt").write_text("old", encoding="utf-8")
        (source / "source-sentinel.txt").write_text("updated", encoding="utf-8")
        second = self.run_installer(source, skills)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(
            (destination / "source-sentinel.txt").read_text(encoding="utf-8"),
            "updated",
        )
        self.assertFalse((destination / "obsolete.txt").exists())

    def test_self_copy_is_refused_without_deleting_source(self) -> None:
        skills = self.root / "claude-skills"
        source = self.make_source(skills / "acc")

        result = self.run_installer(source, skills)

        self.assert_overlap_refused(result)
        self.assertEqual(
            (source / "source-sentinel.txt").read_text(encoding="utf-8"),
            "preserve me",
        )
        self.assertTrue((source / self.script_name).is_file())

    def test_destination_ancestor_is_refused_without_deleting_source(self) -> None:
        skills = self.root / "claude-skills"
        source = self.make_source(skills / "acc" / "checkout")

        result = self.run_installer(source, skills)

        self.assert_overlap_refused(result)
        self.assertEqual(
            (source / "source-sentinel.txt").read_text(encoding="utf-8"),
            "preserve me",
        )

    def test_destination_descendant_is_refused_before_creating_it(self) -> None:
        source = self.make_source(self.root / "checkout")
        skills = source / "nested" / "skills"

        result = self.run_installer(source, skills)

        self.assert_overlap_refused(result)
        self.assertFalse(skills.exists())
        self.assertEqual(
            (source / "source-sentinel.txt").read_text(encoding="utf-8"),
            "preserve me",
        )


@unittest.skipUnless(os.name == "nt", "PowerShell installer is Windows-specific")
class PowerShellInstallerTests(InstallerFixture, unittest.TestCase):
    script_name = "install.ps1"

    @classmethod
    def setUpClass(cls) -> None:
        cls.shell = shutil.which("powershell") or shutil.which("pwsh")
        if cls.shell is None:
            raise unittest.SkipTest("PowerShell is unavailable")

    def run_installer(self, source: Path, skills_dir: Path, *, copy: bool = True):
        command = [
            self.shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(source / self.script_name),
        ]
        if copy:
            command.append("-Copy")
        env = os.environ.copy()
        env["CLAUDE_SKILLS_DIR"] = str(skills_dir)
        return subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)

    def make_junction(self, link: Path, target: Path) -> None:
        escaped_link = str(link).replace("'", "''")
        escaped_target = str(target).replace("'", "''")
        command = (
            f"New-Item -ItemType Junction -Path '{escaped_link}' "
            f"-Target '{escaped_target}' | Out-Null"
        )
        result = subprocess.run(
            [self.shell, "-NoProfile", "-Command", command],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_case_variant_self_copy_is_refused(self) -> None:
        skills = self.root / "Claude-Skills"
        source = self.make_source(skills / "acc")
        case_variant = Path(str(skills).swapcase())

        result = self.run_installer(source, case_variant)

        self.assert_overlap_refused(result)
        self.assertTrue((source / "source-sentinel.txt").is_file())

    def test_copy_reinstall_replaces_junction_entry_without_touching_source(self) -> None:
        source = self.make_source(self.root / "checkout")
        skills = self.root / "claude-skills"
        skills.mkdir()
        self.make_junction(skills / "acc", source)

        result = self.run_installer(source, skills)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((source / "source-sentinel.txt").is_file())
        destination = skills / "acc"
        self.assertFalse(destination.is_symlink())
        self.assertEqual(
            (destination / "source-sentinel.txt").read_text(encoding="utf-8"),
            "preserve me",
        )

    def test_junction_parent_cannot_hide_destination_inside_source(self) -> None:
        source = self.make_source(self.root / "checkout")
        actual_skills = source / "nested" / "skills"
        actual_skills.mkdir(parents=True)
        alias = self.root / "skills-alias"
        self.make_junction(alias, actual_skills)

        result = self.run_installer(source, alias)

        self.assert_overlap_refused(result)
        self.assertFalse((actual_skills / "acc").exists())
        self.assertTrue((source / "source-sentinel.txt").is_file())


class PosixInstallerTests(InstallerFixture, unittest.TestCase):
    script_name = "install.sh"

    @classmethod
    def setUpClass(cls) -> None:
        if os.name == "nt":
            cls.shell = Path(r"C:\Program Files\Git\bin\bash.exe")
            cls.cygpath = Path(r"C:\Program Files\Git\usr\bin\cygpath.exe")
            if not cls.shell.is_file() or not cls.cygpath.is_file():
                raise unittest.SkipTest("Git Bash is unavailable")
        else:
            shell = shutil.which("bash")
            if shell is None:
                raise unittest.SkipTest("bash is unavailable")
            cls.shell = Path(shell)
            cls.cygpath = None

    def shell_path(self, path: Path) -> str:
        if self.cygpath is None:
            return str(path)
        result = subprocess.run(
            [str(self.cygpath), "-u", str(path)],
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()

    def run_installer(self, source: Path, skills_dir: Path, *, copy: bool = True):
        command = [str(self.shell), self.shell_path(source / self.script_name)]
        if copy:
            command.append("--copy")
        env = os.environ.copy()
        env["CLAUDE_SKILLS_DIR"] = self.shell_path(skills_dir)
        return subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)

    def test_symlink_install_and_reinstall(self) -> None:
        source = self.make_source(self.root / "checkout")
        skills = self.root / "claude-skills"

        first = self.run_installer(source, skills, copy=False)
        if os.name == "nt" and first.returncode != 0:
            self.skipTest("native symlink creation is unavailable")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        if os.name == "nt" and not (skills / "acc").is_symlink():
            self.skipTest("Git Bash emulates directory symlinks as copies")
        self.assertTrue((skills / "acc" / "source-sentinel.txt").is_file())

        second = self.run_installer(source, skills, copy=False)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue((source / "source-sentinel.txt").is_file())

    def test_copy_reinstall_replaces_symlink_entry_without_touching_source(self) -> None:
        source = self.make_source(self.root / "checkout")
        skills = self.root / "claude-skills"
        skills.mkdir()
        if os.name == "nt":
            link_command = [
                str(self.shell),
                "-c",
                'MSYS=winsymlinks:nativestrict ln -s "$1" "$2"',
                "install-test",
                self.shell_path(source),
                self.shell_path(skills / "acc"),
            ]
            link = subprocess.run(link_command, text=True, capture_output=True, timeout=30)
            if link.returncode != 0:
                self.skipTest("native symlink creation is unavailable")
        else:
            (skills / "acc").symlink_to(source, target_is_directory=True)

        result = self.run_installer(source, skills)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((source / "source-sentinel.txt").is_file())
        self.assertFalse((skills / "acc").is_symlink())

    @unittest.skipUnless(os.name == "nt", "requires a case-insensitive filesystem")
    def test_case_variant_self_copy_is_refused(self) -> None:
        skills = self.root / "Claude-Skills"
        source = self.make_source(skills / "acc")
        case_variant = Path(str(skills).swapcase())

        result = self.run_installer(source, case_variant)

        self.assert_overlap_refused(result)
        self.assertTrue((source / "source-sentinel.txt").is_file())

    @unittest.skipIf(os.name == "nt", "requires POSIX symlink semantics")
    def test_symlink_parent_cannot_hide_destination_inside_source(self) -> None:
        source = self.make_source(self.root / "checkout")
        actual_skills = source / "nested" / "skills"
        actual_skills.mkdir(parents=True)
        alias = self.root / "skills-alias"
        alias.symlink_to(actual_skills, target_is_directory=True)

        result = self.run_installer(source, alias)

        self.assert_overlap_refused(result)
        self.assertFalse((actual_skills / "acc").exists())
        self.assertTrue((source / "source-sentinel.txt").is_file())


if __name__ == "__main__":
    unittest.main()
