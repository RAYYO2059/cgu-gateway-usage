"""Capture and verify deterministic clean/lite pipeline outputs.

This is an acceptance tool, not part of the production pipeline.  It reads the
already extracted L1/L2 data, rebuilds aggregates and published artifacts under
one run id, and compares only hashes, schemas, file names, and test counts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import (  # noqa: E402
    aggregate,
    aggregate_lite,
    config,
    extract_lite,
    render_lite,
    render_results,
    schema,
)
from src.classify_lite import markers, prefilter  # noqa: E402
from src.metrics import registry, runner  # noqa: E402


MANIFEST_PATH = PROJECT_ROOT / "tools" / "golden" / "manifest.json"
EXPECTED_POPULATION_SHA256 = (
    "63af7d40e051d069dece9f14771eab6be7cb487f25c1937f6ff56ec12eb6919b"
)
DOC_PATHS = (
    Path("README.md"),
    Path("docs/INDEX.md"),
    Path("docs/OVERVIEW.md"),
    Path("docs/RESULTS.md"),
    Path("docs/RESULTS_lite.md"),
)
WHOLE_FILE_DIRS = (
    Path("docs/data"),
    Path("docs/data_lite"),
    Path("docs/figures"),
    Path("docs/figures_lite"),
)
ALLOWED_UNTRACKED_DOCS = {
    "docs/DRAFT_COST_OPTIMIZATION.md",
    "docs/_HANDOFF_2026-09-15.md",
}
AUTOGEN_MARKER = re.compile(
    r"<!-- AUTOGEN:(?P<name>[^:]+):(?P<edge>START|END) -->"
)


class GoldenError(RuntimeError):
    """A comparison cannot be performed safely."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def combined_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(payload)


def relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def extract_autogen_blocks(text: str, label: str = "document") -> dict[str, str]:
    """Return exact content between every AUTOGEN pair, rejecting bad pairs."""
    blocks: dict[str, str] = {}
    open_name: str | None = None
    content_start = 0
    for match in AUTOGEN_MARKER.finditer(text):
        name = match.group("name")
        edge = match.group("edge")
        if edge == "START":
            if open_name is not None:
                raise ValueError(
                    f"{label}: AUTOGEN:{name} starts inside AUTOGEN:{open_name}"
                )
            if name in blocks:
                raise ValueError(f"{label}: duplicate AUTOGEN:{name}")
            open_name = name
            content_start = match.end()
            continue
        if open_name is None:
            raise ValueError(f"{label}: AUTOGEN:{name} END has no START")
        if name != open_name:
            raise ValueError(
                f"{label}: AUTOGEN:{open_name} closed by AUTOGEN:{name}"
            )
        blocks[name] = text[content_start : match.start()]
        open_name = None
    if open_name is not None:
        raise ValueError(f"{label}: AUTOGEN:{open_name} has no END")
    return blocks


def logical_dataframe_hash(frame: pd.DataFrame) -> str:
    """Hash rows independent of row order or parquet partitioning."""
    markers.check_columns(frame)
    row_hashes = pd.util.hash_pandas_object(frame, index=False).to_numpy(
        dtype=np.uint64, copy=True
    )
    row_hashes.sort()
    rows_digest = hashlib.sha256(row_hashes.tobytes()).digest()
    schema_payload = json.dumps(
        [(str(column), str(dtype)) for column, dtype in zip(frame.columns, frame.dtypes)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(schema_payload + b"\0" + rows_digest).hexdigest()


def hash_files(paths: Iterable[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if not path.is_file():
            raise GoldenError(f"required file is missing: {relative(path)}")
        result[relative(path)] = file_sha256(path)
    return result


def hash_files_relative_to(paths: Iterable[Path], base: Path) -> dict[str, str]:
    """Hash files using names relative to a stable base (not a run id)."""
    result: dict[str, str] = {}
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if not path.is_file():
            raise GoldenError(f"required file is missing: {path.name}")
        result[path.relative_to(base).as_posix()] = file_sha256(path)
    return result


def files_in(directories: Iterable[Path], patterns: tuple[str, ...]) -> list[Path]:
    paths: list[Path] = []
    for directory in directories:
        absolute = PROJECT_ROOT / directory
        if not absolute.is_dir():
            raise GoldenError(f"required directory is missing: {directory.as_posix()}")
        for pattern in patterns:
            paths.extend(absolute.glob(pattern))
    return sorted(set(paths), key=lambda item: item.as_posix())


def autogen_hashes() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for relative_path in DOC_PATHS:
        path = PROJECT_ROOT / relative_path
        text = path.read_text(encoding="utf-8")
        blocks = extract_autogen_blocks(text, relative_path.as_posix())
        result[relative_path.as_posix()] = {
            name: sha256_bytes(content.encode("utf-8"))
            for name, content in sorted(blocks.items())
        }
    return result


def whole_doc_hashes() -> dict[str, str]:
    return hash_files(PROJECT_ROOT / item for item in DOC_PATHS)


def environment_versions() -> dict[str, str]:
    result = {"python": platform.python_version()}
    for package in ("pandas", "pyarrow", "numpy", "matplotlib", "pandera"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise GoldenError(f"required package is missing: {package}") from exc
    return result


def environment_differences(
    expected: Mapping[str, str], actual: Mapping[str, str]
) -> dict[str, dict[str, str | None]]:
    differences: dict[str, dict[str, str | None]] = {}
    for name in sorted(set(expected) | set(actual)):
        if expected.get(name) != actual.get(name):
            differences[name] = {
                "expected": expected.get(name),
                "actual": actual.get(name),
            }
    return differences


def compare_file_maps(
    expected: Mapping[str, str], actual: Mapping[str, str]
) -> dict[str, Any]:
    expected_names = set(expected)
    actual_names = set(actual)
    changed = sorted(
        name
        for name in expected_names & actual_names
        if expected[name] != actual[name]
    )
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    return {
        "match": not (changed or missing or extra),
        "changed": changed,
        "missing": missing,
        "extra": extra,
    }


def flatten_blocks(blocks: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    return {
        f"{path}::{name}": digest
        for path, named in blocks.items()
        for name, digest in named.items()
    }


def check_worktree() -> list[dict[str, str]]:
    """Reject tracked or unexpected untracked changes under docs/ and README."""
    command = [
        "git",
        "-c",
        "core.quotepath=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--",
        "docs",
        "README.md",
    ]
    completed = subprocess.run(
        command, cwd=PROJECT_ROOT, check=True, capture_output=True
    )
    entries: list[dict[str, str]] = []
    for raw in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if not raw:
            continue
        status = raw[:2]
        path = raw[3:].replace("\\", "/")
        if status == "??" and path in ALLOWED_UNTRACKED_DOCS:
            continue
        entries.append({"status": status, "path": path})
    return entries


@contextmanager
def registry_for_line(line: str):
    """Temporarily hide the other line from publishers using global REGISTRY."""
    original = dict(registry.REGISTRY)
    registry.REGISTRY.clear()
    registry.REGISTRY.update(
        {name: spec for name, spec in original.items() if spec.line == line}
    )
    try:
        yield
    finally:
        registry.REGISTRY.clear()
        registry.REGISTRY.update(original)


def publish_clean(run_id: str) -> None:
    with registry_for_line("clean"):
        render_results.run(run_id)


def run_pipeline() -> str:
    config.ensure_dirs()
    run_id = config.new_run_id()
    aggregate.run(run_id)
    aggregate_lite.run(run_id)
    runner.run_all(run_id, line="clean")
    publish_clean(run_id)
    runner.run_all(run_id, line="lite")
    render_lite.run(run_id)
    run_dir = config.RUNS_DIR / run_id
    for name in (
        "concentration.csv",
        "concentration_summary.csv",
        "concentration_lite.csv",
    ):
        if not (run_dir / name).is_file():
            raise GoldenError(
                f"current run lacks {name}; fallback output is not acceptable"
            )
    return run_id


def publish_again(run_id: str) -> None:
    publish_clean(run_id)
    render_lite.run(run_id)


def published_hashes() -> dict[str, Any]:
    whole_files = files_in(WHOLE_FILE_DIRS, ("*.csv", "*.png"))
    return {
        "files": hash_files(whole_files),
        "autogen": autogen_hashes(),
        "docs": whole_doc_hashes(),
    }


def logical_table_hashes() -> dict[str, str]:
    tables = {
        "L1_clean": schema.load_dataset(),
        "L1_lite": extract_lite.load_dataset(),
        "L2_turn": pd.read_parquet(aggregate.TURN_PATH),
        "L2_thread": pd.read_parquet(aggregate.THREAD_PATH),
        "L2_user": pd.read_parquet(aggregate.USER_PATH),
        "L2_user_lite": pd.read_parquet(aggregate_lite.USER_LITE_PATH),
    }
    return {name: logical_dataframe_hash(frame) for name, frame in tables.items()}


def classification_hashes() -> dict[str, str]:
    per_sha, _ = prefilter.build()
    keep = ["prompt_text_sha256", "rule", "n_rules_hit", "n_requests"]
    assignment = per_sha[keep].copy()
    markers.check_columns(assignment)
    eligible = sorted(
        assignment.loc[
            assignment["rule"] == prefilter.UNASSIGNED, "prompt_text_sha256"
        ].astype(str)
    )
    population = sha256_bytes("\n".join(eligible).encode("utf-8"))
    return {
        "assignment": logical_dataframe_hash(assignment),
        "eligible_population": population,
    }


def cost_total() -> str:
    path = PROJECT_ROOT / "docs/data_lite/cost_structure_lite.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or "cost_usd" not in rows[0]:
        raise GoldenError("cost_structure_lite.csv lacks cost_usd or data rows")
    return str(rows[0]["cost_usd"])


def pytest_result(run_id: str) -> dict[str, Any]:
    xml_path = config.RUNS_DIR / run_id / "golden_pytest.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-q",
        "-rs",
        f"--junitxml={xml_path}",
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if not xml_path.is_file():
        raise GoldenError(
            f"pytest did not create its result file (exit {completed.returncode})"
        )
    root = ET.parse(xml_path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise GoldenError("pytest XML contains no testsuite")
    total = int(suite.attrib.get("tests", 0))
    failures = int(suite.attrib.get("failures", 0))
    errors = int(suite.attrib.get("errors", 0))
    skipped = int(suite.attrib.get("skipped", 0))
    skip_items: list[dict[str, str]] = []
    for case in suite.iter("testcase"):
        skipped_node = case.find("skipped")
        if skipped_node is None:
            continue
        class_name = case.attrib.get("classname", "")
        node_id = f"{class_name}::{case.attrib.get('name', '')}"
        skip_items.append(
            {
                "node_id": node_id,
                "reason": skipped_node.attrib.get("message", ""),
            }
        )
    return {
        "passed": total - failures - errors - skipped,
        "skipped": skipped,
        "failed": failures + errors,
        "skip_items": sorted(skip_items, key=lambda item: item["node_id"]),
        "exit_code": completed.returncode,
    }


def capture_snapshot(run_id: str) -> dict[str, Any]:
    published = published_hashes()
    run_dir = config.RUNS_DIR / run_id
    concentration_paths = [
        run_dir / name
        for name in (
            "concentration.csv",
            "concentration_summary.csv",
            "concentration_lite.csv",
        )
    ]
    concentration = hash_files_relative_to(concentration_paths, run_dir)
    metric_files = files_in(
        (
            Path("runs") / run_id / "metrics",
            Path("runs") / run_id / "metrics_lite",
        ),
        ("*.csv", "*.suppressed.json"),
    )
    return {
        "A_files": published["files"],
        "B_autogen": published["autogen"],
        "C_docs": published["docs"],
        "D_concentration": concentration,
        "E_metrics": hash_files_relative_to(metric_files, run_dir),
        "F_tables": logical_table_hashes(),
        "G_classification": classification_hashes(),
        "H_cost_total": cost_total(),
        "I_tests": pytest_result(run_id),
    }


def compare_snapshots(
    expected: Mapping[str, Any], actual: Mapping[str, Any], strict_docs: bool
) -> dict[str, Any]:
    categories: dict[str, Any] = {}
    for name in ("A_files", "D_concentration", "E_metrics", "F_tables"):
        categories[name] = compare_file_maps(expected[name], actual[name])
    categories["B_autogen"] = compare_file_maps(
        flatten_blocks(expected["B_autogen"]), flatten_blocks(actual["B_autogen"])
    )
    docs_comparison = compare_file_maps(expected["C_docs"], actual["C_docs"])
    docs_comparison["enforced"] = strict_docs
    categories["C_docs"] = docs_comparison
    categories["G_classification"] = compare_file_maps(
        expected["G_classification"], actual["G_classification"]
    )
    categories["H_cost_total"] = {
        "match": expected["H_cost_total"] == actual["H_cost_total"]
    }
    tests_match = expected["I_tests"] == actual["I_tests"]
    categories["I_tests"] = {
        "match": tests_match and actual["I_tests"]["failed"] == 0
    }
    failed = [
        name
        for name, comparison in categories.items()
        if not comparison["match"] and (name != "C_docs" or strict_docs)
    ]
    return {"match": not failed, "failed_categories": failed, "categories": categories}


def idempotency_comparison(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    files = compare_file_maps(before["files"], after["files"])
    blocks = compare_file_maps(
        flatten_blocks(before["autogen"]), flatten_blocks(after["autogen"])
    )
    docs = compare_file_maps(before["docs"], after["docs"])
    return {
        "match": files["match"] and blocks["match"] and docs["match"],
        "files": files,
        "autogen": blocks,
        "docs": docs,
    }


def item_count(value: Mapping[str, Any]) -> int:
    return len(value)


def difference_lines(label: str, comparison: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for kind in ("missing", "extra", "changed"):
        for name in comparison.get(kind, []):
            lines.append(f"  {label} {kind}: {name}")
    return lines


def print_snapshot_report(
    mode: str,
    run_id: str,
    snapshot: Mapping[str, Any],
    comparison: Mapping[str, Any] | None,
    idempotency: Mapping[str, Any],
    strict_docs: bool,
) -> None:
    print(f"golden {mode}: run_id={run_id}")
    category_values = {
        "A": snapshot["A_files"],
        "B": flatten_blocks(snapshot["B_autogen"]),
        "C": snapshot["C_docs"],
        "D": snapshot["D_concentration"],
        "E": snapshot["E_metrics"],
        "F": snapshot["F_tables"],
        "G": snapshot["G_classification"],
    }
    for label, value in category_values.items():
        state = "CAPTURED"
        if comparison is not None:
            detail = comparison["categories"][
                {
                    "A": "A_files",
                    "B": "B_autogen",
                    "C": "C_docs",
                    "D": "D_concentration",
                    "E": "E_metrics",
                    "F": "F_tables",
                    "G": "G_classification",
                }[label]
            ]
            if label == "C" and not strict_docs:
                state = "REPORT" if detail["match"] else "REPORT-CHANGED"
            else:
                state = "PASS" if detail["match"] else "FAIL"
        print(
            f"{label} {state} count={item_count(value)} "
            f"sha256={combined_sha256(value)[:12]}"
        )
        if comparison is not None:
            lines = difference_lines(label, detail)
            for line in lines:
                print(line)
    cost_state = "CAPTURED"
    test_state = "CAPTURED"
    if comparison is not None:
        cost_state = "PASS" if comparison["categories"]["H_cost_total"]["match"] else "FAIL"
        test_state = "PASS" if comparison["categories"]["I_tests"]["match"] else "FAIL"
    print(f"H {cost_state} cost_usd={snapshot['H_cost_total']}")
    tests = snapshot["I_tests"]
    print(
        f"I {test_state} passed={tests['passed']} skipped={tests['skipped']} "
        f"failed={tests['failed']} skip_items={len(tests['skip_items'])}"
    )
    for item in tests["skip_items"]:
        print(f"  I skip: {item['node_id']} reason={item['reason']}")
    print(
        "IDEMPOTENT "
        f"{'PASS' if idempotency['match'] else 'FAIL'} "
        f"files={len(snapshot['A_files'])} "
        f"blocks={len(flatten_blocks(snapshot['B_autogen']))}"
    )
    for section in ("files", "autogen", "docs"):
        for line in difference_lines(f"IDEMPOTENT/{section}", idempotency[section]):
            print(line)


def compact_report(
    mode: str,
    run_id: str,
    snapshot: Mapping[str, Any],
    comparison: Mapping[str, Any] | None,
    idempotency: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "mode": mode,
        "run_id": run_id,
        "category_counts": {
            "A": len(snapshot["A_files"]),
            "B": len(flatten_blocks(snapshot["B_autogen"])),
            "C": len(snapshot["C_docs"]),
            "D": len(snapshot["D_concentration"]),
            "E": len(snapshot["E_metrics"]),
            "F": len(snapshot["F_tables"]),
            "G": len(snapshot["G_classification"]),
        },
        "category_sha256_prefix": {
            "A": combined_sha256(snapshot["A_files"])[:12],
            "B": combined_sha256(flatten_blocks(snapshot["B_autogen"]))[:12],
            "C": combined_sha256(snapshot["C_docs"])[:12],
            "D": combined_sha256(snapshot["D_concentration"])[:12],
            "E": combined_sha256(snapshot["E_metrics"])[:12],
            "F": combined_sha256(snapshot["F_tables"])[:12],
            "G": combined_sha256(snapshot["G_classification"])[:12],
        },
        "cost_usd": snapshot["H_cost_total"],
        "tests": snapshot["I_tests"],
        "comparison": comparison,
        "idempotency": idempotency,
    }


def execute(mode: str, strict_docs: bool) -> int:
    worktree = check_worktree()
    if worktree:
        print("golden: cannot compare because docs/ or README.md has changes")
        for item in worktree:
            print(f"  {item['status']} {item['path']}")
        return 2

    manifest: dict[str, Any] | None = None
    current_environment = environment_versions()
    if mode == "verify":
        if not MANIFEST_PATH.is_file():
            print(f"golden: manifest is missing: {relative(MANIFEST_PATH)}")
            return 2
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        differences = environment_differences(
            manifest.get("environment", {}), current_environment
        )
        if differences:
            print("golden: environment versions differ; comparison is invalid")
            for package, values in differences.items():
                print(
                    f"  {package}: expected={values['expected']} actual={values['actual']}"
                )
            print("confirm the old environment, then run capture again")
            return 2

    try:
        run_id = run_pipeline()
        first_published = published_hashes()
        publish_again(run_id)
        second_published = published_hashes()
        idempotency = idempotency_comparison(first_published, second_published)
        snapshot = capture_snapshot(run_id)
    except (GoldenError, AssertionError, ValueError, OSError) as exc:
        print(f"golden: cannot compare: {type(exc).__name__}: {exc}")
        return 2

    if snapshot["G_classification"]["eligible_population"] != EXPECTED_POPULATION_SHA256:
        print("golden: eligible population fingerprint differs from the frozen value")
        print(
            "  actual="
            f"{snapshot['G_classification']['eligible_population'][:12]}"
        )
        return 2

    comparison: dict[str, Any] | None = None
    if mode == "capture":
        if not idempotency["match"] or snapshot["I_tests"]["failed"] != 0:
            print_snapshot_report(
                mode, run_id, snapshot, comparison, idempotency, strict_docs
            )
            print("golden: capture refused because acceptance checks failed")
            return 2
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "manifest_version": 1,
            "environment": current_environment,
            "snapshot": snapshot,
        }
        MANIFEST_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        assert manifest is not None
        comparison = compare_snapshots(
            manifest["snapshot"], snapshot, strict_docs=strict_docs
        )

    print_snapshot_report(mode, run_id, snapshot, comparison, idempotency, strict_docs)
    report = compact_report(mode, run_id, snapshot, comparison, idempotency)
    report_path = config.RUNS_DIR / run_id / "golden_verify.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"report={relative(report_path)}")
    if mode == "verify" and comparison is not None and not comparison["match"]:
        return 1
    if not idempotency["match"] or snapshot["I_tests"]["failed"] != 0:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "verify"))
    parser.add_argument(
        "--strict-docs",
        action="store_true",
        help="treat handwritten changes in the five published Markdown files as failures",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return execute(args.command, strict_docs=args.strict_docs)


if __name__ == "__main__":
    raise SystemExit(main())
