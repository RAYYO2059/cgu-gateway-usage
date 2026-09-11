import csv
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT.parent / "_rescued_scratchpad"
sys.path.insert(0, str(ROOT))

from src.classify_lite import judge_codex_blind as cb


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dictionary_examples_match_precommitted_text_exactly():
    expected = {
        "tool_injected": "AI 編碼客戶端在回合交接時自動產生的思考摘要。外框固定,載荷是前一輪的工作狀態。跨數十個不同使用者出現,因為那是客戶端的功能不是任何人寫的東西。沒有人撰寫它,也沒有人閱讀它——它是兩次模型呼叫之間的傳遞。",
        "batch_project": "一個人把一份有界的語料逐項送進同一個模板。例如一份題庫的答案覆核,每題一次呼叫,題目不同而指令完全相同;或一本書的分章摘要。判準是語料由使用者自己提供且有盡頭——送完就結束,不會有新的載荷進來。",
        "service_relay": "使用者架了一個服務,載荷由不特定第三方即時輸入。例如一個校園問答機器人,外框是使用者寫的檢索指令,而每一筆的問題是別人打進去的。gateway 使用者的用途是「營運這個服務」,不是載荷所講的那件事——那些提問者不使用這個 gateway,他們的意圖無從歸屬。",
        "user_envelope": "工具把使用者的材料包起來送出。信封是工具加的(所以跨使用者出現),但裡面裝的是這個使用者自己的檔案、文件或程式碼。判別點是信封佔內容的比例很低而內容很大——外框只有幾十字元,載荷幾千字元。",
        "insufficient": "prompt_text 不含足以判斷的內容。已知成因是上游 schema:lite 的 request 物件沒有 messages/system/instructions,所以指令在 system 而資料在 user 的請求,我們只看得到資料。徵兆是 prompt_tokens 遠大於 prompt_text_len。注意這個值與 user_envelope 在資料上難以分辨——兩者都表現為「有東西不在 prompt_text 裡」,而缺的那部分是工具塞的還是上游拿掉的,中繼資料沒有答案。",
    }
    with (ROOT / "ref" / "label_dictionary.csv").open(
            encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    actual = {
        row["代碼"]: row["具體例子"]
        for row in rows
        if row["層"] == "分流判準" and row["軸"] == "disposition"
        and row["代碼"] in expected
    }
    assert actual == expected


def test_build_prompt_is_byte_identical_to_opus_builder():
    judge = load_module(SCRATCH / "judge_clusters.py", "judge_for_codex_test")
    blind = ROOT.parents[1] / "codex_blind_2026-09"
    manifest = json.loads((blind / "prompt_manifest.json").read_text(encoding="utf-8"))
    row = pd.Series({name: index + 1 for index, name in enumerate(manifest["meta_fields"])})
    row["相異內容數"] = 12
    prefix = "測試用共同開頭"
    got = cb.build_prompt(
        (blind / "prompt_template.txt").read_text(encoding="utf-8"),
        (blind / "axes_block.txt").read_text(encoding="utf-8"),
        manifest["meta_fields"], row, prefix, manifest["prefix_cap"],
    )
    expected = judge.build_prompt(row, prefix, (blind / "axes_block.txt").read_text(encoding="utf-8"))
    assert got.encode("utf-8") == expected.encode("utf-8")


def test_tool_events_count_each_call_once_and_keep_type_only():
    events = [
        {"type": "item.started", "item": {"id": "a", "type": "command_execution", "command": "secret"}},
        {"type": "item.completed", "item": {"id": "a", "type": "command_execution", "aggregated_output": "secret"}},
        {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "ok"}},
        {"type": "item.completed", "item": {"id": "b", "type": "mcp_tool_call", "arguments": {"path": "secret"}}},
    ]
    assert cb.tool_events(events) == [
        {"type": "command_execution", "count": 1},
        {"type": "mcp_tool_call", "count": 1},
    ]


def test_output_contract_has_tool_events_and_no_content_columns():
    assert "tool_events" in cb.OUTPUT_COLUMNS
    assert not set(cb.OUTPUT_COLUMNS) & set(cb.FORBIDDEN_COLUMNS)


def test_reason_overlap_guard():
    prefix = "這是一段不應出現在理由欄位的共同前綴文字而且長度足夠"
    assert cb.reason_leaks_prefix("結構描述：" + prefix, prefix)
    assert not cb.reason_leaks_prefix("外框佔比高，且跨多個使用者。", prefix)


def test_profile_denies_root_and_disables_content_tools():
    assert '":root" = "deny"' in cb.PROFILE_TEXT
    for feature in ("shell_tool", "unified_exec", "apps", "browser_use",
                    "browser_use_external", "computer_use",
                    "workspace_dependencies", "image_generation"):
        assert f"{feature} = false" in cb.PROFILE_TEXT


def test_wrapper_does_not_inject_system_or_developer_prompt():
    source = cb.WRAPPER.read_text(encoding="utf-8")
    assert "system-prompt" not in source
    assert "developer" not in source
    assert "baseInstructions" not in source
    assert "prompt_template.txt" not in source
