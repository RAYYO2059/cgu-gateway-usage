from __future__ import annotations

import copy
import importlib.util
import json
from datetime import datetime
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


# ---------------------------------------------------------------------------
# execute(): exit codes, capture guard, history. No real pipeline is run.
# ---------------------------------------------------------------------------

FIXED_ENV = {"python": "3.12.7", "pandas": "2.3.1"}


def _passing_snapshot() -> dict[str, object]:
    snapshot = _snapshot()
    snapshot["G_classification"] = {
        "assignment": "same",
        "eligible_population": golden.EXPECTED_POPULATION_SHA256,
    }
    return snapshot


def _write_baseline(
    path: Path,
    snapshot: dict[str, object],
    *,
    environment: dict[str, str] = FIXED_ENV,
    history: list[dict[str, object]] | None = None,
) -> bytes:
    payload: dict[str, object] = {
        "manifest_version": 1,
        "environment": environment,
        "snapshot": snapshot,
    }
    if history is not None:
        payload["history"] = history
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(data)
    return data


class FakeRun:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        snapshot: dict[str, object],
        *,
        environment: dict[str, str] = FIXED_ENV,
        dirty: bool = False,
        pipeline_error: Exception | None = None,
    ) -> None:
        self.manifest = tmp_path / "golden" / "manifest.json"
        self.runs = tmp_path / "runs"
        self.snapshot = snapshot
        self.pipeline_error = pipeline_error
        self.pipeline_calls = 0
        self.cr_targets: list[Path] = []
        worktree =[{"status": " M", "path": "docs/OVERVIEW.md"}] if dirty else []
        monkeypatch.setattr(golden, "MANIFEST_PATH", self.manifest)
        monkeypatch.setattr(golden.config, "RUNS_DIR", self.runs)
        monkeypatch.setattr(golden, "check_worktree", lambda: list(worktree))
        monkeypatch.setattr(golden, "environment_versions", lambda: dict(environment))
        monkeypatch.setattr(golden, "run_pipeline", self._pipeline)
        monkeypatch.setattr(
            golden, "line_ending_targets", lambda run_id, snapshot: list(self.cr_targets)
        )
        monkeypatch.setattr(
            golden, "published_hashes", lambda: {"files": {}, "autogen": {}, "docs": {}}
        )
        monkeypatch.setattr(golden, "publish_again", lambda run_id: None)
        monkeypatch.setattr(
            golden, "capture_snapshot", lambda run_id: copy.deepcopy(self.snapshot)
        )
        monkeypatch.setattr(
            golden, "git_state", lambda: {"head": "f" * 40, "tracked_changes": False}
        )

    def _pipeline(self) -> str:
        self.pipeline_calls += 1
        if self.pipeline_error is not None:
            raise self.pipeline_error
        (self.runs / "RUN").mkdir(parents=True, exist_ok=True)
        return "RUN"


def test_verify_returns_one_when_pipeline_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRun(
        monkeypatch, tmp_path, _passing_snapshot(), pipeline_error=ValueError("drill")
    )
    _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("verify", strict_docs=False) == 1
    out = capsys.readouterr().out
    assert "ValueError: drill" in out
    assert "cannot compare" not in out


def test_capture_returns_one_and_keeps_manifest_when_pipeline_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(
        monkeypatch, tmp_path, _passing_snapshot(), pipeline_error=AssertionError("x")
    )
    before = _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("capture", strict_docs=False) == 1
    assert fake.manifest.read_bytes() == before


def test_verify_returns_two_when_worktree_is_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _passing_snapshot(), dirty=True)
    _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("verify", strict_docs=False) == 2
    assert fake.pipeline_calls == 0


def test_verify_returns_zero_for_identical_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _passing_snapshot())
    _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("verify", strict_docs=False) == 0


def test_describe_exception_withholds_pandera_messages() -> None:
    error_type = type("SchemaErrors", (Exception,), {"__module__": "pandera.errors"})
    described = golden.describe_exception(error_type("uid-abc failure cases"))
    assert "pandera.errors.SchemaErrors" in described
    assert "uid-abc" not in described


def _wrong_population() -> dict[str, object]:
    snapshot = _passing_snapshot()
    snapshot["G_classification"] = {
        "assignment": "same",
        "eligible_population": "0" * 64,
    }
    return snapshot


def test_verify_returns_one_when_population_fingerprint_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _wrong_population())
    # the manifest carries the same wrong value, so only the frozen check can fail
    _write_baseline(fake.manifest, _wrong_population())
    assert golden.execute("verify", strict_docs=False) == 1
    assert "population fingerprint differs" in capsys.readouterr().out


@pytest.mark.parametrize("existing", [False, True], ids=("first-capture", "recapture"))
def test_capture_writes_nothing_when_population_fingerprint_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _wrong_population())
    before = _write_baseline(fake.manifest, _wrong_population()) if existing else None
    assert golden.execute("capture", strict_docs=False) == 1
    if existing:
        assert fake.manifest.read_bytes() == before
    else:
        assert not fake.manifest.exists()


def _changed_files_snapshot() -> dict[str, object]:
    snapshot = _passing_snapshot()
    snapshot["A_files"] = {"docs/data/a.csv": "changed"}
    return snapshot


@pytest.mark.parametrize("accept", [[], ["B"], ["B,E"]], ids=("none", "other", "others"))
def test_capture_refuses_unaccepted_difference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    accept: list[str],
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _changed_files_snapshot())
    before = _write_baseline(fake.manifest, _passing_snapshot())
    reason = "drill" if accept else None
    assert golden.execute("capture", strict_docs=False, accept=accept, reason=reason) == 1
    assert fake.manifest.read_bytes() == before
    assert "differ without --accept: A" in capsys.readouterr().out


def test_capture_refuses_unaccepted_handwritten_doc_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = _passing_snapshot()
    changed["C_docs"] = {"README.md": "handwritten"}
    fake = FakeRun(monkeypatch, tmp_path, changed)
    before = _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("capture", strict_docs=False) == 1
    assert fake.manifest.read_bytes() == before


def test_capture_with_accept_and_reason_appends_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _changed_files_snapshot())
    old_entry = {"captured_at": "2026-09-01T00:00:00+08:00", "reason": "first"}
    _write_baseline(fake.manifest, _passing_snapshot(), history=[old_entry])
    assert (
        golden.execute("capture", strict_docs=False, accept=["a"], reason="  drill  ")
        == 0
    )
    written = json.loads(fake.manifest.read_text(encoding="utf-8"))
    assert written["snapshot"]["A_files"] == {"docs/data/a.csv": "changed"}
    assert len(written["history"]) == 2
    assert written["history"][0] == old_entry
    entry = written["history"][1]
    assert entry["accepted"] == ["A"]
    assert entry["reason"] == "drill"
    assert entry["git_head"] == "f" * 40
    assert entry["environment"] == FIXED_ENV
    assert entry["initial"] is False
    assert datetime.fromisoformat(entry["captured_at"]).tzinfo is not None
    assert entry["categories"]["A"]["old"] != entry["categories"]["A"]["new"]
    assert entry["categories"]["B"]["old"] == entry["categories"]["B"]["new"]
    assert all(len(value["new"]) == 12 for value in entry["categories"].values())


def test_capture_appends_to_history_on_repeated_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _changed_files_snapshot())
    _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("capture", strict_docs=False, accept=["A"], reason="one") == 0
    first = json.loads(fake.manifest.read_text(encoding="utf-8"))["history"]
    fake.snapshot = _passing_snapshot()
    assert golden.execute("capture", strict_docs=False, accept=["A"], reason="two") == 0
    second = json.loads(fake.manifest.read_text(encoding="utf-8"))["history"]
    assert second[: len(first)] == first
    assert [item["reason"] for item in second] == ["one", "two"]


@pytest.mark.parametrize("reason", [None, "", "   "], ids=("absent", "empty", "blank"))
def test_capture_accept_without_reason_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str | None
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _changed_files_snapshot())
    before = _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("capture", strict_docs=False, accept=["A"], reason=reason) == 2
    assert fake.manifest.read_bytes() == before
    assert fake.pipeline_calls == 0


def test_accept_rejects_unknown_category_and_verify_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _passing_snapshot())
    before = _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("capture", strict_docs=False, accept=["Z"], reason="x") == 2
    assert golden.execute("verify", strict_docs=False, accept=["A"], reason="x") == 2
    assert fake.manifest.read_bytes() == before
    assert fake.pipeline_calls == 0


def test_first_capture_writes_manifest_with_initial_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _passing_snapshot())
    assert golden.execute("capture", strict_docs=False) == 0
    written = json.loads(fake.manifest.read_text(encoding="utf-8"))
    assert written["snapshot"] == _passing_snapshot()
    assert len(written["history"]) == 1
    assert written["history"][0]["initial"] is True
    assert written["history"][0]["categories"]["A"]["old"] is None


def test_capture_refuses_environment_change_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_env = {**FIXED_ENV, "pandas": "9.9.9"}
    fake = FakeRun(monkeypatch, tmp_path, _passing_snapshot(), environment=new_env)
    before = _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("capture", strict_docs=False) == 2
    assert fake.manifest.read_bytes() == before
    assert fake.pipeline_calls == 0


def test_accept_environment_still_requires_accepting_changed_categories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_env = {**FIXED_ENV, "pandas": "9.9.9"}
    fake = FakeRun(monkeypatch, tmp_path, _changed_files_snapshot(), environment=new_env)
    before = _write_baseline(fake.manifest, _passing_snapshot())
    assert (
        golden.execute(
            "capture", strict_docs=False, accept_environment=True, reason="upgrade"
        )
        == 1
    )
    assert fake.manifest.read_bytes() == before
    assert (
        golden.execute(
            "capture",
            strict_docs=False,
            accept=["A"],
            accept_environment=True,
            reason="upgrade changes PNG",
        )
        == 0
    )
    written = json.loads(fake.manifest.read_text(encoding="utf-8"))
    assert written["environment"] == new_env
    entry = written["history"][-1]
    assert entry["accept_environment"] is True
    assert entry["previous_environment"] == FIXED_ENV
    assert entry["accepted"] == ["A"]


# ---------------------------------------------------------------------------
# I_tests: growth is allowed, shrinkage / failures / skip changes are not.
# ---------------------------------------------------------------------------


def _with_tests(**changes: object) -> dict[str, object]:
    snapshot = _snapshot()
    tests = dict(snapshot["I_tests"])  # type: ignore[arg-type]
    tests.update(changes)
    snapshot["I_tests"] = tests
    return snapshot


SKIP = {"node_id": "tests.test_x::test_y", "reason": "needs data"}


def test_tests_passed_increase_passes_and_reports_delta() -> None:
    comparison = golden.compare_snapshots(
        _with_tests(passed=10), _with_tests(passed=13), strict_docs=False
    )
    assert comparison["match"]
    assert comparison["categories"]["I_tests"]["passed_delta"] == 3


def test_tests_passed_decrease_fails_and_reports_delta() -> None:
    comparison = golden.compare_snapshots(
        _with_tests(passed=10), _with_tests(passed=9), strict_docs=False
    )
    assert comparison["failed_categories"] == ["I_tests"]
    assert comparison["categories"]["I_tests"]["passed_delta"] == -1


def test_tests_extra_skip_fails_and_is_listed() -> None:
    comparison = golden.compare_snapshots(
        _with_tests(), _with_tests(skipped=1, skip_items=[SKIP]), strict_docs=False
    )
    assert comparison["failed_categories"] == ["I_tests"]
    assert comparison["categories"]["I_tests"]["skip_extra"] == [
        "tests.test_x::test_y reason=needs data"
    ]


def test_tests_missing_skip_fails_and_is_listed() -> None:
    comparison = golden.compare_snapshots(
        _with_tests(skipped=1, skip_items=[SKIP]), _with_tests(), strict_docs=False
    )
    assert comparison["failed_categories"] == ["I_tests"]
    assert comparison["categories"]["I_tests"]["skip_missing"] == [
        "tests.test_x::test_y reason=needs data"
    ]


def test_tests_failure_fails_even_when_passed_grows() -> None:
    comparison = golden.compare_snapshots(
        _with_tests(passed=10), _with_tests(passed=11, failed=1), strict_docs=False
    )
    assert comparison["failed_categories"] == ["I_tests"]


def test_tests_exit_code_is_not_compared() -> None:
    comparison = golden.compare_snapshots(
        _with_tests(exit_code=0), _with_tests(exit_code=5), strict_docs=False
    )
    assert comparison["match"]


def test_verify_passes_when_only_new_tests_were_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grown = _passing_snapshot()
    grown["I_tests"] = {**grown["I_tests"], "passed": 50}  # type: ignore[dict-item]
    fake = FakeRun(monkeypatch, tmp_path, grown)
    _write_baseline(fake.manifest, _passing_snapshot())
    assert golden.execute("verify", strict_docs=False) == 0


# ---------------------------------------------------------------------------
# Line endings: text outputs must be LF, independent of the manifest.
# ---------------------------------------------------------------------------

CR = bytes([13])
LF = bytes([10])


def test_carriage_return_files_flags_text_outputs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(golden, "PROJECT_ROOT", tmp_path)
    contents = {
        "crlf.csv": b"a,b" + CR + LF + b"1,2" + CR + LF,
        "lone_cr.json": b"{}" + CR,
        "doc.md": b"text" + CR + LF,
        "clean.csv": b"a,b" + LF + b"1,2" + LF,
        "figure.png": bytes([0x89]) + b"PNG" + CR + LF,
    }
    paths = []
    for name, data in contents.items():
        path = tmp_path / name
        path.write_bytes(data)
        paths.append(path)
    assert golden.carriage_return_files(paths) == ["crlf.csv", "doc.md", "lone_cr.json"]


def test_metric_files_include_both_suppression_sidecars() -> None:
    assert set(golden.METRIC_FILE_PATTERNS) == {
        "*.csv",
        "*.suppressed.json",
        "*.exempted.json",
    }


def test_line_ending_targets_cover_a_d_e_and_doc_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    monkeypatch.setattr(golden, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(golden.config, "RUNS_DIR", runs)
    monkeypatch.setattr(golden, "DOC_PATHS", (Path("README.md"),))
    snapshot = _snapshot()
    snapshot["A_files"] = {"docs/data/a.csv": "x"}
    snapshot["D_concentration"] = {"concentration.csv": "x"}
    snapshot["E_metrics"] = {"metrics/a.suppressed.json": "x"}
    for path in (
        tmp_path / "docs" / "data" / "a.csv",
        runs / "RUN" / "concentration.csv",
        runs / "RUN" / "metrics" / "a.suppressed.json",
        tmp_path / "README.md",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" + CR + LF)
    targets = golden.line_ending_targets("RUN", snapshot)
    assert golden.carriage_return_files(targets) == [
        "README.md",
        "docs/data/a.csv",
        "runs/RUN/concentration.csv",
        "runs/RUN/metrics/a.suppressed.json",
    ]


def _output_file(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "docs" / "data" / "a.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_verify_returns_one_when_outputs_contain_cr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _passing_snapshot())
    monkeypatch.setattr(golden, "PROJECT_ROOT", tmp_path)
    _write_baseline(fake.manifest, _passing_snapshot())
    fake.cr_targets = [_output_file(tmp_path, b"a" + CR + LF)]
    assert golden.execute("verify", strict_docs=False) == 1
    out = capsys.readouterr().out
    assert "LINE-ENDINGS FAIL files_with_cr=1" in out
    assert "LINE-ENDINGS CR: docs/data/a.csv" in out
    assert "outputs must be LF" in out


def test_verify_passes_when_outputs_are_lf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _passing_snapshot())
    _write_baseline(fake.manifest, _passing_snapshot())
    fake.cr_targets = [_output_file(tmp_path, b"a" + LF)]
    assert golden.execute("verify", strict_docs=False) == 0
    assert "LINE-ENDINGS PASS files_with_cr=0" in capsys.readouterr().out


@pytest.mark.parametrize("existing", [False, True], ids=("first-capture", "recapture"))
def test_capture_writes_nothing_when_outputs_contain_cr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    fake = FakeRun(monkeypatch, tmp_path, _changed_files_snapshot())
    before = _write_baseline(fake.manifest, _passing_snapshot()) if existing else None
    fake.cr_targets = [_output_file(tmp_path, b"a" + CR + LF)]
    options = {"accept": ["A"], "reason": "drill"} if existing else {}
    assert golden.execute("capture", strict_docs=False, **options) == 1
    if existing:
        assert fake.manifest.read_bytes() == before
    else:
        assert not fake.manifest.exists()
