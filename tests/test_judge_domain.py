"""逐筆 domain 判定器的 schema、盲化、工具事件與明文邊界。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.classify_lite import judge_domain as judge


def test_schema_只有三個判定欄且沒有_reason():
    schema = judge.build_json_schema()
    assert tuple(schema["properties"]) == (
        "domain", "named_third_party", "confidence")
    assert schema["required"] == ["domain", "named_third_party", "confidence"]
    assert schema["properties"]["domain"]["enum"] == list(judge.DOMAIN_VALUES)
    assert schema["properties"]["named_third_party"] == {"type": "boolean"}
    assert "reason" not in schema["properties"]
    assert "reason" not in judge.OUTPUT_COLUMNS


def test_cli_機械關閉工具與專案設定():
    flags = judge.cli_flags(judge.build_json_schema())
    assert "--safe-mode" in flags
    assert "--restricted" in flags
    assert "--strict-mcp-config" in flags
    assert "--no-session-persistence" in flags
    assert flags[flags.index("--tools") + 1] == ""
    assert flags[flags.index("--permission-prompts") + 1] == "none"
    assert flags[flags.index("--output-format") + 1] == "stream-json"


def test_stream_parser_記錄工具事件類型():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
            {"type": "tool_use", "name": "Read", "input": {"path": "y"}},
        ]}},
        {"type": "result", "is_error": False,
         "structured_output": {
             "domain": "general", "named_third_party": False,
             "confidence": "high",
         },
         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 3,
                   "cache_read_input_tokens": 5, "output_tokens": 7}},
    ]
    value, tools, usage = judge.parse_stream(
        "\n".join(json.dumps(event) for event in events))
    assert judge.parse_judgement(value)["domain"] == "general"
    assert tools == ["Read", "Read"]
    assert usage["prompt_tokens_total"] == 10


def test_output_guard_擋同一次與跨_write_的內容():
    secret = "這是一段不應該出現在輸出的逐筆敏感內容而且長度足夠"
    guard = judge.OutputGuard(io.StringIO(), n=4)
    guard.watch(secret)
    with pytest.raises(SystemExit, match="OutputGuard"):
        guard.write("逐筆敏感內容")

    guard = judge.OutputGuard(io.StringIO(), n=4)
    guard.watch(secret)
    guard.write("逐筆敏")
    with pytest.raises(SystemExit, match="OutputGuard"):
        guard.write("感內容")


def test判定解析拒絕多欄與非布林():
    with pytest.raises(ValueError, match="欄位"):
        judge.parse_judgement({
            "domain": "general", "named_third_party": False,
            "confidence": "high", "reason": "x",
        })
    with pytest.raises(ValueError, match="布林"):
        judge.parse_judgement({
            "domain": "general", "named_third_party": "false",
            "confidence": "high",
        })


def test輸出只含雜湊判定與操作欄(tmp_path):
    row = {column: "" for column in judge.OUTPUT_COLUMNS}
    row.update({
        "prompt_text_sha256": "a" * 64,
        "domain": "general",
        "named_third_party": "false",
        "confidence": "high",
        "model": judge.MODEL,
        "prompt_sha256": "b" * 64,
        "schema_version": judge.SCHEMA_VERSION,
        "tool_events": "[]",
    })
    out = tmp_path / "out.csv"
    judge.append_output(out, row)
    saved = pd.read_csv(out, encoding="utf-8-sig", dtype=str,
                        keep_default_na=False)
    assert tuple(saved.columns) == judge.OUTPUT_COLUMNS
    assert not judge.FORBIDDEN_OUTPUT_COLUMNS & set(saved.columns)


def test第三人索引只含_sha與_source_path(tmp_path):
    sample = pd.DataFrame([
        {"prompt_text_sha256": "a" * 64, "source_path": "d/a.json"},
        {"prompt_text_sha256": "b" * 64, "source_path": "d/b.json"},
    ])
    output = tmp_path / "out.csv"
    pd.DataFrame([
        {"prompt_text_sha256": "a" * 64, "named_third_party": "true"},
        {"prompt_text_sha256": "b" * 64, "named_third_party": "false"},
    ]).to_csv(output, index=False, encoding="utf-8-sig")
    sidecar = tmp_path / "third.csv"
    assert judge.rebuild_third_party_index(output, sample, sidecar) == 1
    saved = pd.read_csv(sidecar, encoding="utf-8-sig", dtype=str)
    assert tuple(saved.columns) == judge.THIRD_PARTY_COLUMNS
    assert saved.iloc[0].to_dict() == {
        "prompt_text_sha256": "a" * 64,
        "source_path": "d/a.json",
    }


def test指紋涵蓋字典內容與_schema():
    schema = judge.build_json_schema()
    first = judge.prompt_fingerprint("domain definitions v1", schema)
    assert first == judge.prompt_fingerprint("domain definitions v1", schema)
    assert first != judge.prompt_fingerprint("domain definitions v2", schema)
    changed = json.loads(json.dumps(schema))
    changed["properties"]["confidence"]["enum"].append("unsure")
    assert first != judge.prompt_fingerprint("domain definitions v1", changed)


def test_domain_沒有_must_約束檢查():
    schema = judge.build_json_schema()
    assert "disposition" not in schema["properties"]
    assert "axis_level" not in schema["properties"]
    source = Path(judge.__file__).read_text(encoding="utf-8")
    assert "must_constraint_violation" not in source
    assert "assert_constraints_representable" not in source
