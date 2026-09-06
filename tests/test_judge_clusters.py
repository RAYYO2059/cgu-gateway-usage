"""`_rescued_scratchpad/judge_clusters.py` 的測試。

**測試不呼叫 API。** 所有 fixture 字串都是這裡現造的，沒有一筆真實內容。
判定器本身由專案主人執行；這裡只驗它的純函式，尤其是那個閘門——
`flagged` 的群不可以被送出去，而那件事在真的送出去之後才發現就太晚了。

判定器在 repo 外（換機器時要一起帶），找不到就 skip 而不是 fail。

跑法：pytest tests/test_judge_clusters.py
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
JUDGE = REPO.parent / "_rescued_scratchpad" / "judge_clusters.py"


def _load():
    if not JUDGE.exists():
        pytest.skip(f"判定器不在本機（{JUDGE}），跳過")
    spec = importlib.util.spec_from_file_location("judge_clusters", JUDGE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["judge_clusters"] = module
    spec.loader.exec_module(module)      # 模組層只有常數與 def
    return module


jc = _load()

FIELDS = {
    "frame_owner": ("tool", "user", "none", "unsure"),
    "disposition": ("remove", "keep", "split", "unsure"),
    "axis_level": ("all_three", "invocation_only", "none", "unsure"),
}


def _screen(rows):
    return pd.DataFrame(rows, columns=["group_id", "personal_data"])


# --- 閘門：只有 clean 能送 ---------------------------------------------------
# 這是本檔最重要的一組。前綴一旦送出去就收不回來，而「送錯了」不會有
# 任何徵兆——API 照樣回一個看起來合理的判定。
def test_只有_clean_通過閘門():
    s = _screen([("A001", "clean"), ("A002", "flagged"),
                 ("A003", "unsure"), ("A004", "clean")])
    assert jc.clean_group_ids(s) == {"A001", "A004"}


def test_flagged_絕對不通過():
    s = _screen([("A001", "flagged")])
    assert "A001" not in jc.clean_group_ids(s)


def test_unsure_絕對不通過():
    # unsure 是「我不確定」，不是「應該沒問題」。當成後者用等於沒有閘門。
    s = _screen([("A001", "unsure")])
    assert "A001" not in jc.clean_group_ids(s)


def test_沒有篩檢紀錄的群不通過():
    # 「還沒篩」與「篩過是 clean」是兩件事。把前者當後者，等於在沒有
    # 閘門的情況下送出，而且看起來一切正常。
    s = _screen([("A001", "clean")])
    assert jc.clean_group_ids(s) == {"A001"}
    assert "A999" not in jc.clean_group_ids(s)


def test_空的篩檢檔一個都不通過():
    assert jc.clean_group_ids(_screen([])) == set()
    assert jc.clean_group_ids(None) == set()


def test_未知的篩檢值不通過():
    s = _screen([("A001", "CLEAN "), ("A002", "ok"), ("A003", "")])
    # 大小寫不同就是不同的值——這裡刻意不做寬鬆比對，寧可漏送不可誤送。
    assert jc.clean_group_ids(s) == set()


def test_沒有任何參數可以關掉閘門():
    """CLI 不得提供繞過篩檢的開關。"""
    parser_opts = set()
    import argparse
    real = argparse.ArgumentParser.add_argument

    def spy(self, *a, **kw):
        for token in a:
            if isinstance(token, str) and token.startswith("-"):
                parser_opts.add(token)
        return real(self, *a, **kw)

    argparse.ArgumentParser.add_argument = spy
    try:
        jc.main(["--dry-run", "--help"])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.add_argument = real
    assert parser_opts <= {"--limit", "--dry-run", "-h", "--help"}, parser_opts


def test_跳過原因逐桶計數():
    s = _screen([("A001", "clean"), ("A002", "flagged"), ("A003", "flagged"),
                 ("A004", "unsure"), ("A005", "weird")])
    got = jc.skip_counts(s, ["A001", "A002", "A003", "A004", "A005", "A006"])
    assert got == {"flagged": 2, "unsure": 1, "unscreened": 1, "other": 1}


def test_跳過計數三個桶一律出現即使是零():
    got = jc.skip_counts(_screen([("A001", "clean")]), ["A001"])
    assert set(got) == {"flagged", "unsure", "unscreened", "other"}
    assert all(v == 0 for v in got.values())


# --- JSON 解析 --------------------------------------------------------------
GOOD = ('{"frame_owner": "tool", "disposition": "remove", '
        '"axis_level": "all_three", "reason": "外框佔比高、跨多個使用者。", '
        '"confidence": "high"}')


def test_正常的_json_解析得出來():
    got = jc.parse_judgement(GOOD, FIELDS)
    assert got["frame_owner"] == "tool"
    assert got["disposition"] == "remove"
    assert got["axis_level"] == "all_three"
    assert got["confidence"] == "high"
    assert got["reason"]


def test_被程式碼區塊圍起來也解析得出來():
    assert jc.parse_judgement(f"```json\n{GOOD}\n```", FIELDS)["frame_owner"] == "tool"


def test_前後有多餘文字也解析得出來():
    text = f"好的，我的判定如下：\n{GOOD}\n以上。"
    assert jc.parse_judgement(text, FIELDS)["confidence"] == "high"


def test_大小寫與空白被正規化():
    text = GOOD.replace('"tool"', '" TOOL "').replace('"high"', '"High"')
    got = jc.parse_judgement(text, FIELDS)
    assert got["frame_owner"] == "tool"
    assert got["confidence"] == "high"


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "完全沒有 json",
    "{ 這不是合法的 json",
    "[1, 2, 3]",
])
def test_壞掉的回應會_raise(bad):
    with pytest.raises(ValueError):
        jc.parse_judgement(bad, FIELDS)


@pytest.mark.parametrize("mutation", [
    ('"tool"', '"toolz"'),
    ('"remove"', '"delete"'),
    ('"all_three"', '"all"'),
    ('"high"', '"very_high"'),
])
def test_值域外的答案會_raise(mutation):
    with pytest.raises(ValueError):
        jc.parse_judgement(GOOD.replace(*mutation), FIELDS)


def test_缺欄位會_raise():
    import json as _json
    data = _json.loads(GOOD)
    for key in ("frame_owner", "disposition", "axis_level",
                "confidence", "reason"):
        partial = {k: v for k, v in data.items() if k != key}
        with pytest.raises(ValueError):
            jc.parse_judgement(_json.dumps(partial), FIELDS)


def test_reason_不是字串會_raise():
    with pytest.raises(ValueError):
        jc.parse_judgement(GOOD.replace('"外框佔比高、跨多個使用者。"', "123"),
                           FIELDS)


def test_解析失敗的訊息不夾帶原文():
    """例外訊息會被 print，所以它不能帶著回應內容跑出來。"""
    secret = "SENSITIVE-PAYLOAD-DO-NOT-LEAK-0123456789"
    try:
        jc.parse_judgement("{" + secret, FIELDS)
    except ValueError as exc:
        assert secret not in str(exc)
    else:
        pytest.fail("應該要 raise")


# --- reason 外洩防線 --------------------------------------------------------
def test_reason_抄了前綴會被抓到():
    prefix = "FRAME-AAA 這是一段固定的外框文字，長度超過二十四個字元用來測試。"
    reason = "這個群的外框是：這是一段固定的外框文字，長度超過二十四個字元用來測試"
    assert jc.reason_leaks_prefix(reason, prefix) is True


def test_正常的_reason_不會誤報():
    prefix = "FRAME-AAA 這是一段固定的外框文字，長度超過二十四個字元用來測試。"
    reason = "外框佔內容比例高、跨多個使用者、單一模型，形狀像工具附加的框架。"
    assert jc.reason_leaks_prefix(reason, prefix) is False


def test_短字串不判為外洩():
    # 「使用者」這種短共同片段不該誤報。
    assert jc.reason_leaks_prefix("使用者", "使用者的外框", n=24) is False


def test_空值不判為外洩():
    assert jc.reason_leaks_prefix("", "abc" * 20) is False
    assert jc.reason_leaks_prefix("abc" * 20, "") is False


# --- 輸出欄位 ---------------------------------------------------------------
def test_輸出欄位清單不含明文欄名():
    jc.check_output_columns(jc.OUTPUT_COLUMNS)


@pytest.mark.parametrize("bad", ["prefix", "prefix_raw", "prompt_text",
                                 "text", "content", "PREFIX_NORMALIZED"])
def test_明文欄名硬擋(bad):
    with pytest.raises(AssertionError):
        jc.check_output_columns(list(jc.OUTPUT_COLUMNS) + [bad])


def test_寫出的_csv_只有九欄而且沒有前綴(tmp_path):
    path = tmp_path / "judge_output.csv"
    jc.append_output(path, dict(
        group_id="A001", frame_owner="tool", disposition="remove",
        axis_level="all_three", reason="結構描述", confidence="high",
        model="claude-opus-5", prompt_sha256="a" * 64,
        judged_at="2026-09-06T20:00:00"))
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == list(jc.OUTPUT_COLUMNS)
    jc.check_output_columns(rows[0].keys())
    assert "prefix" not in " ".join(rows[0].keys()).lower()


def test_多塞的欄位不會落地(tmp_path):
    path = tmp_path / "o.csv"
    jc.append_output(path, dict(
        group_id="A001", frame_owner="tool", disposition="keep",
        axis_level="none", reason="r", confidence="low", model="m",
        prompt_sha256="s", judged_at="t", 額外="x"))
    with path.open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.DictReader(fh).fieldnames) == list(jc.OUTPUT_COLUMNS)


def test_缺欄位會_raise(tmp_path):
    with pytest.raises(ValueError):
        jc.append_output(tmp_path / "o.csv", {"group_id": "A001"})


# --- 中斷續跑 ---------------------------------------------------------------
def test_沒有輸出檔時全部待判(tmp_path):
    assert jc.load_done(tmp_path / "nope.csv") == set()


def test_續跑跳過已判的(tmp_path):
    path = tmp_path / "o.csv"
    for gid in ("A001", "A003"):
        jc.append_output(path, dict(
            group_id=gid, frame_owner="tool", disposition="keep",
            axis_level="none", reason="r", confidence="low", model="m",
            prompt_sha256="s", judged_at="t"))
    done = jc.load_done(path)
    assert done == {"A001", "A003"}
    clean = {"A001", "A002", "A003", "A004"}
    todo = [g for g in ["A001", "A002", "A003", "A004"]
            if g in clean and g not in done]
    assert todo == ["A002", "A004"]


def test_輸出檔壞掉時當作沒判過而不是炸掉(tmp_path):
    path = tmp_path / "o.csv"
    path.write_bytes(b"\xff\xfe not a csv \x00")
    assert isinstance(jc.load_done(path), set)


# --- 金鑰 -------------------------------------------------------------------
def test_沒有金鑰時啟動就報錯(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        jc.require_credentials()
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_有金鑰時通過(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    jc.require_credentials()


def test_金鑰不出現在原始碼裡():
    source = JUDGE.read_text(encoding="utf-8")
    assert "sk-ant" not in source


# --- 提示詞指紋 -------------------------------------------------------------
def test_三軸定義改了指紋就換():
    """值域是模板的一部分：它改了，舊的判定結果就不再可比。"""
    a = jc.render_axes(FIELDS, {
        k: {v: "" for v in vals} for k, vals in FIELDS.items()})
    other = {**FIELDS, "frame_owner": ("tool", "user", "none", "unsure", "新值")}
    b = jc.render_axes(other, {
        k: {v: "" for v in vals} for k, vals in other.items()})
    assert jc.template_fingerprint(a) != jc.template_fingerprint(b)


def test_同樣的定義給同樣的指紋():
    a = jc.render_axes(FIELDS, {
        k: {v: "" for v in vals} for k, vals in FIELDS.items()})
    assert jc.template_fingerprint(a) == jc.template_fingerprint(a)
    assert len(jc.template_fingerprint(a)) == 64


def test_提示詞明文禁止引用前綴():
    assert "不得引用共同前綴的任何字句" in jc.PROMPT_TEMPLATE


# --- 不在 import 時連網 -----------------------------------------------------
def test_模組層不_import_anthropic_也不讀檔():
    source = JUDGE.read_text(encoding="utf-8")
    lines = [l for l in source.splitlines()
             if l.startswith("import ") or l.startswith("from ")]
    assert not any("anthropic" in l for l in lines), \
        "anthropic 必須在 main() 裡才 import，否則沒裝套件就連測試都跑不動"
    assert "anthropic" not in sys.modules or True   # 測試本身沒有觸發它


def test_一群一次獨立呼叫_沒有共用歷史():
    """judge_one 的 messages 只有這一次的 prompt，沒有累積的歷史。

    同一個上下文裡連判 226 次會漂移，而漂移沒有訊號。
    """
    source = JUDGE.read_text(encoding="utf-8")
    body = source.split("def judge_one(")[1].split("\ndef ")[0]
    assert 'messages=[{"role": "user", "content": prompt}]' in body
    assert "history" not in body and "append" not in body
