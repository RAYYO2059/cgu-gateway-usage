from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "golden.py"
SPEC = importlib.util.spec_from_file_location("golden_tool", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
golden = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(golden)


def test_extract_autogen_blocks_reads_multiple_blocks_and_markdown_table() -> None:
    text = """before
<!-- AUTOGEN:FIRST:START -->
| a | b |
| - | - |
| 1 | 2 |
<!-- AUTOGEN:FIRST:END -->
middle
<!-- AUTOGEN:LAST:START -->last body<!-- AUTOGEN:LAST:END -->
after
"""
    blocks = golden.extract_autogen_blocks(text, "sample.md")
    assert list(blocks) == ["FIRST", "LAST"]
    assert "| 1 | 2 |" in blocks["FIRST"]
    assert blocks["LAST"] == "last body"


def test_extract_autogen_blocks_rejects_missing_end() -> None:
    with pytest.raises(ValueError, match="has no END"):
        golden.extract_autogen_blocks(
            "<!-- AUTOGEN:OPEN:START -->unfinished", "sample.md"
        )


def test_extract_autogen_blocks_rejects_mismatched_pair() -> None:
    with pytest.raises(ValueError, match="closed by"):
        golden.extract_autogen_blocks(
            "<!-- AUTOGEN:A:START -->x<!-- AUTOGEN:B:END -->", "sample.md"
        )


def test_logical_hash_is_row_order_independent() -> None:
    frame = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    shuffled = frame.sample(frac=1, random_state=9).reset_index(drop=True)
    assert golden.logical_dataframe_hash(frame) == golden.logical_dataframe_hash(shuffled)


def test_logical_hash_is_partition_independent() -> None:
    frame = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    reconstructed = pd.concat([frame.iloc[2:], frame.iloc[:2]], ignore_index=True)
    assert golden.logical_dataframe_hash(frame) == golden.logical_dataframe_hash(
        reconstructed
    )


@pytest.mark.parametrize(
    "changed",
    [
        pd.DataFrame({"a": [1, 2, 4], "b": ["x", "y", "z"]}),
        pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": ["x", "y", "z"]}),
        pd.DataFrame({"a": [1, 2, 3, 3], "b": ["x", "y", "z", "z"]}),
    ],
    ids=("cell", "dtype", "duplicate-row"),
)
def test_logical_hash_changes_when_table_changes(changed: pd.DataFrame) -> None:
    original = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    assert golden.logical_dataframe_hash(original) != golden.logical_dataframe_hash(changed)


def test_logical_hash_rejects_plaintext_column() -> None:
    frame = pd.DataFrame({"prompt_text": ["not printed"]})
    with pytest.raises(AssertionError, match="明文欄位"):
        golden.logical_dataframe_hash(frame)


def test_run_file_hash_names_do_not_include_run_id(tmp_path: Path) -> None:
    first = tmp_path / "run-a"
    second = tmp_path / "run-b"
    (first / "metrics").mkdir(parents=True)
    (second / "metrics").mkdir(parents=True)
    first_file = first / "metrics" / "result.csv"
    second_file = second / "metrics" / "result.csv"
    first_file.write_bytes(b"same")
    second_file.write_bytes(b"same")
    assert golden.hash_files_relative_to([first_file], first) == golden.hash_files_relative_to(
        [second_file], second
    ) == {"metrics/result.csv": golden.sha256_bytes(b"same")}


@pytest.mark.parametrize(
    ("expected", "actual", "kind", "name"),
    [
        ({"a": "1"}, {"a": "1", "b": "2"}, "extra", "b"),
        ({"a": "1", "b": "2"}, {"a": "1"}, "missing", "b"),
        ({"a": "1"}, {"a": "2"}, "changed", "a"),
    ],
    ids=("extra-file", "missing-file", "changed-byte"),
)
def test_compare_file_maps_reports_each_difference(
    expected: dict[str, str], actual: dict[str, str], kind: str, name: str
) -> None:
    comparison = golden.compare_file_maps(expected, actual)
    assert not comparison["match"]
    assert comparison[kind] == [name]


def _snapshot(*, docs_hash: str = "same") -> dict[str, object]:
    return {
        "A_files": {"docs/data/a.csv": "same"},
        "B_autogen": {"README.md": {"BLOCK": "same"}},
        "C_docs": {"README.md": docs_hash},
        "D_concentration": {"runs/R/concentration.csv": "same"},
        "E_metrics": {"runs/R/metrics/a.csv": "same"},
        "F_tables": {"L1_clean": "same"},
        "G_classification": {
            "assignment": "same",
            "eligible_population": "same",
        },
        "H_cost_total": "2749.32",
        "I_tests": {
            "passed": 1,
            "skipped": 0,
            "failed": 0,
            "skip_items": [],
            "exit_code": 0,
        },
    }


def test_handwritten_doc_change_passes_non_strict_and_fails_strict() -> None:
    expected = _snapshot(docs_hash="old")
    actual = _snapshot(docs_hash="new")
    non_strict = golden.compare_snapshots(expected, actual, strict_docs=False)
    strict = golden.compare_snapshots(expected, actual, strict_docs=True)
    assert non_strict["match"]
    assert not strict["match"]
    assert strict["failed_categories"] == ["C_docs"]


def test_autogen_change_fails_even_without_strict_docs() -> None:
    expected = _snapshot()
    actual = _snapshot()
    actual["B_autogen"] = {"README.md": {"BLOCK": "changed"}}
    comparison = golden.compare_snapshots(expected, actual, strict_docs=False)
    assert not comparison["match"]
    assert comparison["failed_categories"] == ["B_autogen"]


def test_environment_version_difference_is_not_comparable() -> None:
    differences = golden.environment_differences(
        {"python": "3.12.1", "pandas": "2.2.0"},
        {"python": "3.12.1", "pandas": "2.3.0"},
    )
    assert differences == {
        "pandas": {"expected": "2.2.0", "actual": "2.3.0"}
    }


def test_environment_missing_package_is_not_comparable() -> None:
    differences = golden.environment_differences(
        {"python": "3.12.1", "pandas": "2.2.0"},
        {"python": "3.12.1"},
    )
    assert differences["pandas"] == {"expected": "2.2.0", "actual": None}


def test_verify_returns_exit_two_when_environment_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "manifest.json"
    versions = golden.environment_versions()
    versions["pandas"] = "different-version"
    manifest.write_text(
        golden.json.dumps({"environment": versions, "snapshot": {}}, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr(golden, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(golden, "check_worktree", lambda: [])
    assert golden.execute("verify", strict_docs=False) == 2
    assert "environment versions differ" in capsys.readouterr().out
