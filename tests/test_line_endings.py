"""Static guard: text writes in src/ and tools/ must pin their line ending.

The golden CR check only fires on Windows: on Linux a missing lineterminator
still produces LF, so the omission would go unnoticed until someone runs the
pipeline on Windows. This test reads the source instead, so it fails on every
platform.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (ROOT / "src", ROOT / "tools")
EXCLUDED_DIRS = (ROOT / "src" / "classify_lite",)
WRITE_MODE_CHARS = set("wax+")


def scanned_files() -> list[Path]:
    files: list[Path] = []
    for base in SCAN_DIRS:
        for path in sorted(base.rglob("*.py")):
            if any(path.is_relative_to(excluded) for excluded in EXCLUDED_DIRS):
                continue
            files.append(path)
    return files


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _open_mode(call: ast.Call) -> str | None:
    """Literal mode of open()/Path.open(); None when not statically known."""
    for keyword in call.keywords:
        if keyword.arg == "mode":
            node: ast.expr | None = keyword.value
            break
    else:
        # builtin open(file, mode) vs Path.open(mode)
        index = 1 if isinstance(call.func, ast.Name) else 0
        node = call.args[index] if len(call.args) > index else None
    if node is None:
        return "r"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def violations(source: str, label: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
        where = f"{label}:{node.lineno}"
        if name == "to_csv" and "lineterminator" not in keywords:
            found.append(f"{where} to_csv without lineterminator")
        elif name == "write_text" and "newline" not in keywords:
            found.append(f"{where} write_text without newline")
        elif name == "open":
            mode = _open_mode(node)
            if mode is None:
                found.append(f"{where} open with a mode that is not a literal")
            elif (
                "b" not in mode
                and WRITE_MODE_CHARS & set(mode)
                and "newline" not in keywords
            ):
                found.append(f"{where} text-mode open({mode!r}) without newline")
    return found


def test_scan_covers_src_and_tools_but_not_classify_lite() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in scanned_files()}
    assert "tools/golden.py" in names
    assert "src/identity.py" in names
    assert "src/metrics/runner.py" in names
    assert not any(name.startswith("src/classify_lite/") for name in names)


def test_text_writes_in_src_and_tools_pin_line_endings() -> None:
    found: list[str] = []
    for path in scanned_files():
        label = path.relative_to(ROOT).as_posix()
        found.extend(violations(path.read_text(encoding="utf-8"), label))
    assert not found, "text writes must pin LF:\n" + "\n".join(found)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("df.to_csv(path, index=False)", "to_csv without lineterminator"),
        ("path.write_text(text, encoding='utf-8')", "write_text without newline"),
        ("open(path, 'w', encoding='utf-8')", "text-mode open('w') without newline"),
        ("path.open('a', encoding='utf-8')", "text-mode open('a') without newline"),
        ("path.open(mode='r+')", "text-mode open('r+') without newline"),
        ("path.open(chosen_mode)", "open with a mode that is not a literal"),
    ],
    ids=("to_csv", "write_text", "builtin-open-w", "path-open-a", "keyword-mode", "dynamic-mode"),
)
def test_violations_flag_unpinned_writes(source: str, expected: str) -> None:
    assert violations(source, "sample.py") == [f"sample.py:1 {expected}"]


@pytest.mark.parametrize(
    "source",
    [
        "df.to_csv(path, index=False, lineterminator=LF)",
        "path.write_text(text, encoding='utf-8', newline=LF)",
        "path.open('w', encoding='utf-8-sig', newline='')",
        "path.open('rb')",
        "path.open('wb')",
        "path.open(encoding='utf-8')",
        "open(path)",
    ],
    ids=("to_csv", "write_text", "open-w", "read-binary", "write-binary", "read-default", "builtin-read"),
)
def test_violations_accept_pinned_or_non_text_writes(source: str) -> None:
    assert violations(source, "sample.py") == []


def test_violation_reports_file_and_line() -> None:
    source = "import pandas\n\n\ndf.to_csv(path)\n"
    assert violations(source, "src/example.py") == [
        "src/example.py:4 to_csv without lineterminator"
    ]
