#!/usr/bin/env python3
"""Inspect an ACC archive without modifying it or exposing checkpoint bodies.

The report describes a best-effort observation, not an atomic archive snapshot.
Exit codes are 0 for healthy, missing, or informational results; 1 for archive
findings; and 2 for invalid invocation or incomplete inspection. No lockfile is
opened, and entry symlinks are never followed for content.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Inspection must also leave a bundle placed inside the target archive alone.
# Prevent importing the shared helper from creating __pycache__ beside it.
sys.dont_write_bytecode = True

try:
    from acc_archive import (
        draft_parts,
        entry_parts,
        is_recognizably_unfinished_text,
        validate_checkpoint,
    )
except ModuleNotFoundError:  # Imported by path from the repository test suite.
    from scripts.acc_archive import (
        draft_parts,
        entry_parts,
        is_recognizably_unfinished_text,
        validate_checkpoint,
    )

MAX_FINDINGS = 200
MAX_ENTRIES = 10000
MAX_ENTRY_BYTES = 2 * 1024 * 1024
MAX_TOTAL_READ_BYTES = 64 * 1024 * 1024


class _EntryChanged(OSError):
    """The entry no longer matches the regular file originally observed."""


def _is_reparse_point(details: os.stat_result) -> bool:
    return bool(getattr(details, "st_file_attributes", 0) & 0x400)


def _fingerprint(details: os.stat_result) -> tuple:
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)


def _open_nofollow(path: Path) -> int:
    """Open a file read-only, retaining symlink/reparse-point identity.

    Windows does not expose O_NOFOLLOW, so use its equivalent flag before
    transferring handle ownership to a normal Python file descriptor.
    """
    if os.name != "nt":
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    # GENERIC_READ, share read/write/delete, OPEN_EXISTING, OPEN_REPARSE_POINT.
    handle = create_file(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close_handle = kernel.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
        raise


def _read_regular_text(path: Path, expected: os.stat_result) -> str:
    descriptor = _open_nofollow(path)
    with os.fdopen(descriptor, "rb", buffering=0) as handle:
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or _fingerprint(opened) != _fingerprint(expected)
        ):
            raise _EntryChanged()
        content = handle.read(expected.st_size)
        after_read = os.fstat(handle.fileno())
    current = path.lstat()
    if (
        stat.S_ISLNK(current.st_mode)
        or _is_reparse_point(current)
        or _fingerprint(current) != _fingerprint(expected)
        or _fingerprint(after_read) != _fingerprint(expected)
        or len(content) != expected.st_size
    ):
        raise _EntryChanged()
    return content.decode("utf-8")


def _new_report(directory: Path) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "archive": str(directory),
        "status": "healthy",
        "latest": None,
        "selection_complete": True,
        "consistency": "best_effort",
        "counts": {"finals": 0, "drafts": 0, "load_eligible": 0, "ignored": 0},
        "entries": [],
        "findings": [],
        "omitted_findings": 0,
    }


def _add_finding(
    report: Dict[str, Any],
    code: str,
    severity: str,
    message: str,
    path: Optional[str] = None,
) -> None:
    if severity == "error":
        report["status"] = "error"
        report["selection_complete"] = False
    elif severity == "warning" and report["status"] != "error":
        report["status"] = "findings"
    if len(report["findings"]) < MAX_FINDINGS:
        report["findings"].append(
            {"code": code, "severity": severity, "path": path, "message": message}
        )
    else:
        report["omitted_findings"] += 1


def inspect_archive(acc_dir: Path) -> Dict[str, Any]:
    """Return deterministic schema-v1 diagnostics without creating any files.

    Existing legacy content keeps the same loading eligibility as the archive
    readers. Strict publication-format findings are reported separately.
    Bounds or detected concurrent changes suppress authoritative selection.
    """
    directory = Path(os.path.abspath(str(acc_dir)))
    report = _new_report(directory)
    try:
        directory = directory.resolve()
        report["archive"] = str(directory)
        before = directory.stat()
    except FileNotFoundError:
        report["status"] = "missing"
        _add_finding(report, "archive_missing", "info", "No archive exists at this location.")
        return report
    except (OSError, RuntimeError):
        _add_finding(report, "archive_unreadable", "error", "The archive cannot be inspected.")
        return report
    if not stat.S_ISDIR(before.st_mode):
        _add_finding(
            report, "archive_not_directory", "error", "The archive location is not a directory."
        )
        return report
    try:
        with os.scandir(directory) as children:
            paths = [
                directory / child.name for child in itertools.islice(children, MAX_ENTRIES + 1)
            ]
    except OSError:
        _add_finding(report, "archive_unreadable", "error", "The archive cannot be listed.")
        return report
    if len(paths) > MAX_ENTRIES:
        _add_finding(
            report,
            "archive_limit_exceeded",
            "error",
            f"The archive exceeds the inspection limit of {MAX_ENTRIES} directory entries.",
        )
        return report

    completed: Dict[int, List[Dict[str, Any]]] = {}
    remaining_read_bytes = MAX_TOTAL_READ_BYTES
    for path in sorted(
        paths,
        key=lambda candidate: (
            (entry_parts(candidate) or draft_parts(candidate) or (0, "", ""))[0],
            candidate.name,
        ),
    ):
        final = entry_parts(path)
        draft = draft_parts(path)
        try:
            details = path.lstat()
        except FileNotFoundError:
            _add_finding(
                report, "archive_changed", "error", "An entry disappeared during inspection."
            )
            continue
        except OSError:
            _add_finding(
                report, "entry_unreadable", "warning", "Entry metadata cannot be read.", path.name
            )
            if final or draft:
                _add_finding(
                    report,
                    "inspection_incomplete",
                    "error",
                    "Checkpoint type could not be established.",
                    path.name,
                )
            continue
        if stat.S_ISLNK(details.st_mode) or _is_reparse_point(details):
            _add_finding(
                report, "entry_symlink", "warning", "Link or reparse point skipped.", path.name
            )
            if (
                (final or draft)
                and not stat.S_ISLNK(details.st_mode)
                and stat.S_ISREG(details.st_mode)
            ):
                parts = final or draft
                assert parts is not None
                report["counts"]["finals" if final else "drafts"] += 1
                report["entries"].append(
                    {
                        "name": path.name,
                        "kind": "final" if final else "draft",
                        "sequence": parts[0],
                        "load_eligible": None if final else False,
                    }
                )
                if final:
                    _add_finding(
                        report,
                        "inspection_incomplete",
                        "error",
                        "Non-symlink file reparse point cannot be inspected safely.",
                        path.name,
                    )
                else:
                    _add_finding(
                        report,
                        "draft_present",
                        "info",
                        "Draft excluded from automatic loading.",
                        path.name,
                    )
            else:
                report["counts"]["ignored"] += 1
            continue
        if not final and not draft:
            report["counts"]["ignored"] += 1
            continue
        if not stat.S_ISREG(details.st_mode):
            report["counts"]["ignored"] += 1
            _add_finding(
                report,
                "entry_not_file",
                "warning",
                "Checkpoint name is not a regular file.",
                path.name,
            )
            continue
        parts = final or draft
        assert parts is not None
        entry = {
            "name": path.name,
            "kind": "final" if final else "draft",
            "sequence": parts[0],
            "load_eligible": False,
        }
        report["entries"].append(entry)
        report["counts"]["finals" if final else "drafts"] += 1
        if draft:
            _add_finding(
                report, "draft_present", "info", "Draft excluded from automatic loading.", path.name
            )
            continue
        if details.st_size > MAX_ENTRY_BYTES:
            entry["load_eligible"] = None
            _add_finding(
                report,
                "entry_limit_exceeded",
                "error",
                f"Entry exceeds the inspection limit of {MAX_ENTRY_BYTES} bytes.",
                path.name,
            )
            continue
        if details.st_size > remaining_read_bytes:
            entry["load_eligible"] = None
            _add_finding(
                report,
                "archive_read_limit_exceeded",
                "error",
                f"Entry would exceed the total read budget of {MAX_TOTAL_READ_BYTES} bytes.",
                path.name,
            )
            continue
        # Reserve the advertised bytes even if the read fails. Reads never
        # exceed this size, so the aggregate content-read bound is enforced.
        remaining_read_bytes -= details.st_size
        try:
            text = _read_regular_text(path, details)
        except (_EntryChanged, FileNotFoundError):
            entry["load_eligible"] = None
            _add_finding(
                report, "archive_changed", "error", "Entry changed during inspection.", path.name
            )
            continue
        except UnicodeError:
            _add_finding(
                report,
                "entry_invalid_utf8",
                "warning",
                "Entry is not valid UTF-8; excluded.",
                path.name,
            )
            continue
        except OSError:
            _add_finding(
                report, "entry_unreadable", "warning", "Entry cannot be read; excluded.", path.name
            )
            continue
        if is_recognizably_unfinished_text(text):
            _add_finding(
                report,
                "unfinished_final",
                "warning",
                "Recognizable unfinished checkpoint excluded from automatic loading.",
                path.name,
            )
        else:
            entry["load_eligible"] = True
            completed.setdefault(parts[0], []).append(entry)
        structural = validate_checkpoint(text)
        if structural:
            _add_finding(
                report,
                "legacy_structure",
                "warning",
                "Publication-format findings (do not independently change loading eligibility): "
                + "; ".join(structural),
                path.name,
            )

    eligible: List[Dict[str, Any]] = []
    for sequence in sorted(completed):
        entries = completed[sequence]
        if len(entries) > 1:
            for entry in entries:
                entry["load_eligible"] = False
            _add_finding(
                report,
                "duplicate_sequence",
                "warning",
                f"{len(entries)} completed entries claim sequence {sequence}; "
                "automatic latest selection skips this sequence.",
            )
        else:
            eligible.extend(entries)
    report["counts"]["load_eligible"] = len(eligible)
    try:
        after = directory.stat()
        if _fingerprint(after) != _fingerprint(before):
            _add_finding(
                report, "archive_changed", "error", "The archive changed during inspection."
            )
    except OSError:
        _add_finding(
            report, "archive_changed", "error", "The archive disappeared during inspection."
        )
    if report["selection_complete"] and eligible:
        report["latest"] = eligible[-1]["name"]
    return report


def render_text(report: Dict[str, Any]) -> str:
    """Render only metadata and fixed diagnostics, escaping unsafe filename text."""

    def quoted(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True)

    latest = quoted(report["latest"]) if report["latest"] is not None else "none"
    counts = report["counts"]
    lines = [
        f"ACC archive: {quoted(report['archive'])}",
        f"Status: {report['status']}",
        f"Selected latest: {latest}",
        f"Entries: {counts['finals']} finals, {counts['drafts']} drafts, "
        f"{counts['load_eligible']} load-eligible, {counts['ignored']} ignored",
        "Observation: best effort; concurrent changes can be missed (not an atomic snapshot).",
    ]
    if not report["selection_complete"]:
        lines.append("Latest selection is unavailable because inspection was incomplete.")
    for finding in report["findings"]:
        location = f" {quoted(finding['path'])}:" if finding["path"] is not None else ""
        lines.append(
            f"{finding['severity'].upper()} {finding['code']}{location} {finding['message']}"
        )
    if report["omitted_findings"]:
        lines.append(f"Additional findings omitted: {report['omitted_findings']}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect an ACC archive without changing it.")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--dir", help="Archive directory (default: docs/acc under cwd).")
    where.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="Use the cross-project archive (~/.claude/acc, or $ACC_GLOBAL_DIR).",
    )
    parser.add_argument("--json", action="store_true", help="Emit deterministic schema-v1 JSON.")
    args = parser.parse_args(argv)
    if args.use_global:
        directory = Path(os.environ.get("ACC_GLOBAL_DIR") or Path.home() / ".claude" / "acc")
    else:
        directory = Path(args.dir or "docs/acc")
    report = inspect_archive(directory)
    output = (
        json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2)
        if args.json
        else render_text(report)
    )
    print(output)
    return {"healthy": 0, "missing": 0, "findings": 1, "error": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
