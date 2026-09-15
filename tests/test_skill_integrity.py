#!/usr/bin/env python3
"""Integrity checks for the skill bundle itself (not the scripts' logic).

These guard the contracts that make the *shipped skill* coherent, the kind
of breakage unit tests on `scripts/` won't catch:

  * SKILL.md has valid frontmatter with the keys the loader needs.
  * Every bundled file SKILL.md advertises actually exists on disk.
  * Every `{{TOKEN}}` that new_acc.py substitutes is present in the
    template — otherwise substitution silently no-ops and users get a
    literal `{{DATE}}` in their ACC.

Stdlib-only (no PyYAML), so they run anywhere Python does, same as the
rest of the suite:

    python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "SKILL.md"
TEMPLATE = REPO_ROOT / "assets" / "acc-template.md"
NEW_ACC_SRC = REPO_ROOT / "scripts" / "new_acc.py"
PRE_COMPACT_SETTINGS = REPO_ROOT / "assets" / "pre-compact-settings.json"

# Code-span paths in SKILL.md that point at bundled files.
BUNDLED_PATH_RE = re.compile(r"`((?:scripts|assets|references)/[\w./-]+\.(?:py|md))`")
# First argument of every `.replace("{{TOKEN}}", ...)` call in new_acc.py.
SUBSTITUTED_TOKEN_RE = re.compile(r'\.replace\(\s*"(\{\{[A-Z_]+\}\})"')


def _split_frontmatter(text: str) -> dict[str, str]:
    """Parse the leading `---` YAML block as flat key: value pairs.

    Deliberately minimal (no nesting/lists) — the ACC frontmatter is flat,
    and avoiding PyYAML keeps the suite dependency-free.
    """
    if not text.startswith("---"):
        raise AssertionError("SKILL.md must open with a '---' frontmatter fence")
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise AssertionError("SKILL.md frontmatter is not closed with '---'")
    fm: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip()
    return fm


class FrontmatterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fm = _split_frontmatter(SKILL_MD.read_text(encoding="utf-8"))

    def test_has_required_keys(self) -> None:
        for key in ("name", "description", "user-invocable"):
            self.assertIn(key, self.fm, f"frontmatter missing '{key}'")

    def test_name_is_acc(self) -> None:
        self.assertEqual(self.fm["name"], "acc")

    def test_user_invocable_true(self) -> None:
        self.assertEqual(self.fm["user-invocable"].lower(), "true")

    def test_description_non_empty(self) -> None:
        self.assertTrue(self.fm["description"].strip())

    def test_description_is_a_json_compatible_quoted_yaml_scalar(self) -> None:
        # JSON double-quoted strings are valid YAML scalars. This catches the
        # previous unquoted colon-space sequences without adding PyYAML as a
        # runtime or test dependency.
        raw = self.fm["description"]
        self.assertTrue(raw.startswith('"') and raw.endswith('"'))
        self.assertIsInstance(json.loads(raw), str)


class PreCompactDistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = json.loads(PRE_COMPACT_SETTINGS.read_text(encoding="utf-8"))

    def test_example_commands_are_snapshot_only(self) -> None:
        groups = self.settings["hooks"]["PreCompact"]
        commands = [hook["command"] for group in groups for hook in group["hooks"]]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all("acc_pre_compact.py" in command for command in commands))
        self.assertTrue(all("--snapshot-only" not in command for command in commands))

    def test_example_uses_a_hook_shell_variable(self) -> None:
        text = PRE_COMPACT_SETTINGS.read_text(encoding="utf-8")
        self.assertIn("$HOME", text)
        self.assertNotIn("%USERPROFILE%", text)


class SessionStartDistributionTests(unittest.TestCase):
    def test_example_uses_a_hook_shell_variable(self) -> None:
        text = (REPO_ROOT / "assets" / "session-start-settings.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("$HOME", text)
        self.assertNotIn("%USERPROFILE%", text)


class BundledResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL_MD.read_text(encoding="utf-8")
        self.referenced = sorted(set(BUNDLED_PATH_RE.findall(self.text)))

    def test_finds_some_bundled_paths(self) -> None:
        # Guards the parser itself: if this regex stops matching, the
        # existence check below would pass vacuously.
        self.assertGreaterEqual(len(self.referenced), 4)

    def test_every_referenced_file_exists(self) -> None:
        missing = [p for p in self.referenced if not (REPO_ROOT / p).is_file()]
        self.assertEqual(missing, [], f"SKILL.md references missing files: {missing}")

    def test_core_bundle_is_referenced(self) -> None:
        for path in (
            "scripts/new_acc.py",
            "scripts/find_latest_acc.py",
            "assets/acc-template.md",
            "assets/docs-acc-readme.md",
            "assets/global-acc-readme.md",
        ):
            self.assertIn(path, self.referenced, f"SKILL.md no longer documents {path}")


class TemplateTokenContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template_text = TEMPLATE.read_text(encoding="utf-8")
        src = NEW_ACC_SRC.read_text(encoding="utf-8")
        self.substituted = set(SUBSTITUTED_TOKEN_RE.findall(src))

    def test_script_substitutes_expected_tokens(self) -> None:
        # Pin the contract so a refactor that drops a replace() is caught.
        self.assertEqual(self.substituted, {"{{DATE}}", "{{FOCUS}}"})

    def test_every_substituted_token_present_in_template(self) -> None:
        # If the script replaces a token the template lacks, substitution
        # is a silent no-op and the rendered ACC keeps the literal token.
        absent = [t for t in self.substituted if t not in self.template_text]
        self.assertEqual(absent, [], f"template missing tokens the script substitutes: {absent}")

    def test_model_filled_tokens_present_in_template(self) -> None:
        # These are left for the model to fill (not script-substituted), but
        # must exist in the skeleton or the prompt's instructions dangle.
        for token in ("{{TOKENS_BEFORE}}", "{{TOKENS_AFTER}}"):
            self.assertIn(token, self.template_text)


if __name__ == "__main__":
    unittest.main()
