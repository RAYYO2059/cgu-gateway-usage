from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest


REPO = Path(__file__).resolve().parents[1]
CARRIER_PATH = REPO.parent / "_rescued_scratchpad" / "review_named_third_party.py"


def _load_carrier():
    if not CARRIER_PATH.exists():
        pytest.skip("repo-external named-third-party carrier is absent")
    spec = importlib.util.spec_from_file_location("review_named_third_party", CARRIER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def carrier():
    return _load_carrier()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_row(carrier, text: str = "synthetic") -> dict[str, str]:
    return {
        "prompt_text_sha256": _sha(text),
        "named_third_party": "no",
        "elapsed_sec": "1.2",
        "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def test_frozen_limits_and_independent_order(carrier):
    assert carrier.EXPECTED_ROWS == 36
    assert carrier.CONTENT_CAP == 4_000
    assert carrier.BATCH_SIZE == 12
    assert carrier.MAX_BATCHES == 3
    assert carrier.REVIEW_ORDER_SEED not in {20260911, 2026091101, 2026091102}
    rows = [
        {"prompt_text_sha256": f"{i:064x}", "source_path": f"{i}.json"}
        for i in range(36)
    ]
    first = carrier.shuffled_review_list(rows)
    second = carrier.shuffled_review_list(rows)
    assert first == second
    assert first != rows
    assert sorted(row["prompt_text_sha256"] for row in first) == sorted(
        row["prompt_text_sha256"] for row in rows
    )


def test_review_list_requires_only_sha_and_source(carrier, tmp_path):
    path = tmp_path / "list.csv"
    rows = [
        {"prompt_text_sha256": f"{i:064x}", "source_path": f"d/{i}.json"}
        for i in range(3)
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=carrier.INPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    assert carrier.load_review_list(path, expected_rows=3) == rows

    path.write_text("prompt_text_sha256,source_path,stratum\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        carrier.load_review_list(path, expected_rows=0)


def test_output_is_exactly_four_non_text_columns(carrier, tmp_path):
    path = tmp_path / "reviews.csv"
    row = _valid_row(carrier)
    carrier.append_review(row, path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == carrier.OUTPUT_COLUMNS
        assert list(reader) == [row]
    assert carrier.OUTPUT_COLUMNS == (
        "prompt_text_sha256",
        "named_third_party",
        "elapsed_sec",
        "reviewed_at",
    )
    forbidden = {"note", "reason", "prompt_text", "content", "source_path"}
    assert not forbidden.intersection(carrier.OUTPUT_COLUMNS)

    with pytest.raises(ValueError, match="exactly"):
        carrier.append_review({**row, "note": "must never be written"}, path)


@pytest.mark.parametrize("answer", ["yes", "no", "unsure"])
def test_only_three_answers_are_valid(carrier, answer):
    row = _valid_row(carrier, answer)
    row["named_third_party"] = answer
    carrier.validate_output_row(row)


@pytest.mark.parametrize("answer", ["", "true", "false", "maybe"])
def test_blank_and_non_frozen_answers_are_invalid(carrier, answer):
    row = _valid_row(carrier, answer or "blank")
    row["named_third_party"] = answer
    with pytest.raises(ValueError, match="answer"):
        carrier.validate_output_row(row)


def test_interactive_blank_is_not_an_answer(carrier, monkeypatch, capsys):
    answers = iter(["", "unsure"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert carrier._ask_answer() == "unsure"
    assert "空白不是答案" in capsys.readouterr().out


def test_resume_rejects_wrong_schema_and_duplicate_hash(carrier, tmp_path):
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("prompt_text_sha256,named_third_party,note\n", encoding="utf-8")
    with pytest.raises(ValueError, match="four frozen columns"):
        carrier.load_completed(wrong)

    duplicate = tmp_path / "duplicate.csv"
    row = _valid_row(carrier)
    with duplicate.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=carrier.OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerow(row)
        writer.writerow(row)
    with pytest.raises(ValueError, match="duplicate"):
        carrier.load_completed(duplicate)


def test_content_window_marks_truncation(carrier):
    exact = "x" * 4_000
    shown, truncated = carrier.content_window(exact)
    assert shown == exact
    assert truncated is False
    shown, truncated = carrier.content_window(exact + "y")
    assert shown == exact
    assert truncated is True


def test_stdout_and_stderr_guards_share_secrets(carrier):
    out = io.StringIO()
    err = io.StringIO()
    stdout_guard = carrier.OutputGuard(out, n=8)
    stderr_guard = carrier.OutputGuard(err, n=8)
    stderr_guard._secrets = stdout_guard._secrets
    secret = "synthetic-sensitive-content"
    stdout_guard.watch(secret)
    with pytest.raises(SystemExit, match="OutputGuard"):
        stdout_guard.write(secret)
    with pytest.raises(SystemExit, match="OutputGuard"):
        stderr_guard.write(secret)
    assert secret not in out.getvalue()
    assert secret not in err.getvalue()


def test_intentional_display_is_the_only_plaintext_bypass(carrier):
    out = io.StringIO()
    guard = carrier.OutputGuard(out, n=8)
    text = "synthetic review text"
    guard.watch(text)
    carrier.display_prompt_text(guard, text)
    assert out.getvalue() == text + "\n"
    with pytest.raises(SystemExit, match="OutputGuard"):
        guard.write(text)


def test_build_metadata_uses_request_multiplicity_without_plaintext(carrier):
    sha = _sha("metadata-only")
    requests = pd.DataFrame(
        {
            "prompt_text_sha256": [sha, sha],
            "prompt_text_len": [13, 13],
            "anonymous_user_id": ["u1", "u2"],
            "provider": ["p", "p"],
            "request_style": ["r", "r"],
            "model_returned": ["m1", "m2"],
            "date_taipei": ["2026-09-01", "2026-09-02"],
        }
    )
    users = pd.DataFrame(
        {"anonymous_user_id": ["u1", "u2"], "unit_type": ["學生", "教職員"]}
    )
    result = carrier.build_metadata(requests, users, {sha})[sha]
    assert result["length"] == 13
    assert result["duplicate_count"] == 2
    assert result["model_returned"] == "m1 / m2"
    assert result["date_taipei"] == "2026-09-01 / 2026-09-02"

    requests["prompt_text"] = "must be rejected"
    with pytest.raises(AssertionError, match="must not contain"):
        carrier.build_metadata(requests, users, {sha})


def test_load_prompt_text_checks_root_and_hash(carrier, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    text = "entirely synthetic prompt"
    path = raw / "one.json"
    path.write_text(json.dumps({"request": {"prompt_text": text}}), encoding="utf-8")
    assert carrier.load_prompt_text("one.json", _sha(text), raw_root=raw) == text
    with pytest.raises(ValueError, match="sha256"):
        carrier.load_prompt_text("one.json", "0" * 64, raw_root=raw)
    with pytest.raises(ValueError, match="leaves"):
        carrier.load_prompt_text("../outside.json", _sha(text), raw_root=raw)


def test_import_does_not_execute_carrier(carrier, tmp_path):
    assert callable(carrier.main)
    assert not (tmp_path / "named_third_party_review.csv").exists()
