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
import io as _io
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


ROW = dict(
    group_id="A001", frame_owner="tool", disposition="remove",
    axis_level="all_three", reason="結構描述", confidence="high",
    model="claude-opus-5", prompt_sha256="a" * 64,
    judged_at="2026-09-06T20:00:00",
    total_cost_usd=0.0276, input_tokens=2, output_tokens=349,
    thinking_tokens=92, cache_creation_input_tokens=812,
    cache_read_input_tokens=0, prompt_tokens_total=814,
    schema_version="v2", duration_ms=6695)

# v1 的一列：沒有那三個 token 欄位，也沒有版本欄。
ROW_V1 = {k: v for k, v in ROW.items()
          if k in ("group_id", "frame_owner", "disposition", "axis_level",
                   "reason", "confidence", "model", "prompt_sha256",
                   "judged_at", "total_cost_usd", "input_tokens",
                   "output_tokens", "thinking_tokens", "duration_ms")}


def _write_v1(path, rows):
    """照 v1 的表頭寫一個舊格式的輸出檔。"""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(jc.OUTPUT_COLUMNS_V1))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in jc.OUTPUT_COLUMNS_V1})


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
    # --only 在名單裡，理由是它只會讓 todo 變短：apply_only 取的是與
    # clean 名單的交集。新增選項要先想清楚它能不能讓 todo 變長，
    # 能的話就是繞過閘門，不論它叫什麼名字。
    # `--output` 只決定寫到哪個檔，不決定送哪些群——閘門在 clean_group_ids()
    # 與 apply_only()，它碰不到。加進白名單是刻意的。
    assert parser_opts <= {"--limit", "--dry-run", "--prefix-field", "--only",
                           "--output",
                           "-h", "--help"}, parser_opts
    # 而且沒有任何選項的名字聽起來像在繞過閘門。
    for opt in parser_opts:
        for word in ("screen", "skip", "force", "all", "unsafe", "flagged",
                     "no-gate", "bypass"):
            assert word not in opt.lower(), opt


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


def test_寫出的_csv_欄位固定而且沒有前綴(tmp_path):
    path = tmp_path / "judge_output.csv"
    jc.append_output(path, dict(ROW))
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == list(jc.OUTPUT_COLUMNS)
    jc.check_output_columns(rows[0].keys())
    assert "prefix" not in " ".join(rows[0].keys()).lower()


def test_多塞的欄位不會落地(tmp_path):
    path = tmp_path / "o.csv"
    jc.append_output(path, {**ROW, "額外": "x"})
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
        jc.append_output(path, {**ROW, "group_id": gid})
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
def test_找不到_claude_時啟動就報錯(monkeypatch):
    monkeypatch.setattr(jc.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        jc.require_claude_cli()
    assert "claude" in str(exc.value)


def test_找得到_claude_時通過(monkeypatch):
    monkeypatch.setattr(jc.shutil, "which", lambda name: "/usr/bin/claude")
    assert jc.require_claude_cli() == "/usr/bin/claude"


def test_不再需要_API_金鑰():
    """走 headless 就是為了不另外計費。原始碼裡不該再有金鑰的路徑。"""
    source = JUDGE.read_text(encoding="utf-8")
    assert "sk-ant" not in source
    code = source.split("from __future__ import annotations", 1)[1]
    # 說明文字裡提到「不需要 ANTHROPIC_API_KEY」是可以的；**讀它**不行。
    assert "os.environ" not in code
    assert "getenv" not in code
    assert "import anthropic" not in code
    assert "import os" not in code.split("import pandas")[0]


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
    """每次都是全新的 claude -p，沒有 --resume / --continue / session 累積。

    同一個上下文裡連判 226 次會漂移，而漂移沒有訊號。
    """
    source = JUDGE.read_text(encoding="utf-8")
    code = source.split("from __future__ import annotations", 1)[1]
    for bad in ("--resume", "--continue", "--fork-session", "session_id="):
        assert bad not in code, bad
    assert "--no-session-persistence" in code


# --- 前綴欄位選擇（--prefix-field）------------------------------------------
# raw 是同群成員逐字相同的那一段；normalized 是數字換成佔位、空白收斂之後
# 才取的共同前綴，因此更長。更長不等於更好——normalized 已經不是任何一筆
# 真實內容的開頭，被抹掉的數字可能正是線索。哪個好要量測，所以做成參數。
def test_預設送的是_raw():
    """預設值不可改。改了就是在沒有量測的情況下換材料。"""
    assert jc.DEFAULT_PREFIX_FIELD == "raw"
    assert jc.prefix_column(jc.DEFAULT_PREFIX_FIELD) == "prefix_raw"


def test_值域只有兩個而且對到正確欄名():
    assert set(jc.PREFIX_FIELDS) == {"raw", "normalized"}
    assert jc.prefix_column("raw") == "prefix_raw"
    assert jc.prefix_column("normalized") == "prefix_normalized"


@pytest.mark.parametrize("bad", ["", "RAW", "prefix_raw", "norm", "both", None])
def test_未知的前綴欄位被擋下(bad):
    with pytest.raises((ValueError, TypeError)):
        jc.prefix_column(bad)


def test_換前綴欄位會換指紋():
    """prefix_field 是模板的一部分：送 raw 與送 normalized 是兩份不同的材料。

    不換指紋的話，兩批不可比的結果會混在同一個 judge_output.csv 裡，
    而且事後分不出來哪一列是哪一批。
    """
    axes = jc.render_axes(FIELDS, {
        k: {v: "" for v in vals} for k, vals in FIELDS.items()})
    a = jc.template_fingerprint(axes, "raw")
    b = jc.template_fingerprint(axes, "normalized")
    assert a != b
    assert len(a) == len(b) == 64


def test_指紋的預設參數就是預設欄位():
    axes = jc.render_axes(FIELDS, {
        k: {v: "" for v in vals} for k, vals in FIELDS.items()})
    assert jc.template_fingerprint(axes) == \
        jc.template_fingerprint(axes, jc.DEFAULT_PREFIX_FIELD)


def test_指紋對未知欄位會_raise_而不是靜默算一個():
    axes = jc.render_axes(FIELDS, {
        k: {v: "" for v in vals} for k, vals in FIELDS.items()})
    with pytest.raises(ValueError):
        jc.template_fingerprint(axes, "both")


def test_CLI_接受兩個值且預設為_raw():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prefix-field", choices=sorted(jc.PREFIX_FIELDS),
                        default=jc.DEFAULT_PREFIX_FIELD)
    assert parser.parse_args([]).prefix_field == "raw"
    assert parser.parse_args(["--prefix-field", "normalized"]).prefix_field \
        == "normalized"
    with pytest.raises(SystemExit):
        parser.parse_args(["--prefix-field", "both"])


def test_送出的欄位由參數決定_只有一處讀_parquet():
    """程式裡只能有一個地方決定送哪一欄，否則參數會漏掉其中一處。"""
    source = JUDGE.read_text(encoding="utf-8")
    # 取 prefix 的那一行必須用變數，不得寫死欄名
    assert 'prefix = str(prefixes.loc[gid, column])' in source
    assert 'prefixes.loc[gid, "prefix_raw"]' not in source
    assert 'prefixes.loc[gid, "prefix_normalized"]' not in source


def test_外洩防線對兩欄都比對():
    """送哪一欄都要對兩欄比對——raw 與 normalized 只差數字與空白，
    抄了其中一段往往兩邊都命中，而漏判的代價是明文進 CSV。"""
    source = JUDGE.read_text(encoding="utf-8")
    assert "for col in PREFIX_FIELDS.values()" in source


# --- 盲性 -------------------------------------------------------------------
# 人工審閱不得在看過 LLM 判定之後進行。兩支程式互不讀取，是為了讓「沒有
# 互看」由檔案依賴關係保證，而不是靠人記得。規則寫在
# ref/annotation_protocol.md 第二節〈盲性〉。
def test_判定器不讀人工審閱結果():
    source = JUDGE.read_text(encoding="utf-8")
    code = source.split("from __future__ import annotations", 1)[1]
    assert "cluster_review" not in code, \
        "判定器碰到了人工審閱結果——那會讓一致率變成『模型有多會抄』"


def test_判定器只讀這四個檔():
    source = JUDGE.read_text(encoding="utf-8")
    paths = [l.split("=")[0].strip() for l in source.splitlines()
             if "HERE /" in l and "=" in l]
    assert set(paths) == {"PREFIXES", "CLUSTERS", "SCREEN_CSV",
                          "OUTPUT_CSV", "CARRIER"}


def test_從載具只取定義不取判定():
    """_load_carrier 是為了三軸的值域，不是為了讀人的答案。"""
    source = JUDGE.read_text(encoding="utf-8")
    body = source.split("def _load_carrier(")[1].split("\nPROMPT_TEMPLATE")[0]
    assert "FIELDS" not in body or "review_clusters" in body
    assert "cluster_review" not in body
    assert "REVIEW_CSV" not in body


def test_main_的待判清單只取通過閘門的群():
    """閘門的最後一哩：`clean_group_ids()` 算得對，不代表 main 有用它。

    這一行原本沒有測試覆蓋——突變測試裡那條「沒篩檢的群當成通過」一直
    回報「突變無效」，追下去才發現是它沒被驗到，不是它不會壞。
    """
    source = JUDGE.read_text(encoding="utf-8")
    assert "todo = [g for g in all_ids if g in clean and g not in done]" in source, \
        "main 組待判清單的方式變了——確認它仍然只取通過閘門的群"


# ===========================================================================
# Claude Code headless 呼叫層
# ===========================================================================
# subprocess 全部 mock，**不真的呼叫 claude**。fixture 字串都是現造的。
import json as _json
import types as _types


def _envelope(result, **over):
    env = {
        "type": "result", "subtype": "success", "is_error": False,
        "num_turns": 1, "result": result, "total_cost_usd": 0.0066,
        "duration_ms": 1660,
        "usage": {"input_tokens": 2, "output_tokens": 151,
                  "cache_creation_input_tokens": 812,
                  "cache_read_input_tokens": 0,
                  "output_tokens_details": {"thinking_tokens": 0}},
    }
    env.update(over)
    return _json.dumps(env, ensure_ascii=False)


class _FakeRun:
    """記錄 subprocess.run 收到什麼，回傳預先安排好的信封。"""

    def __init__(self, outputs, returncode=0):
        self.outputs = list(outputs)
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": list(argv), **kw})
        out = self.outputs.pop(0) if self.outputs else ""
        return _types.SimpleNamespace(returncode=self.returncode,
                                      stdout=out, stderr="")


def _axes():
    return jc.render_axes(FIELDS, {k: {v: "" for v in vals}
                                   for k, vals in FIELDS.items()})


# --- 提示詞走 stdin，不進命令列 ---------------------------------------------
def test_提示詞走_stdin_不進命令列(monkeypatch, tmp_path):
    """前綴含換行與引號，塞進命令列參數會被參數解析改掉，而且不會報錯。"""
    fake = _FakeRun([_envelope("{}")])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    prompt = 'FRAME "含引號"\n含換行\n'
    jc.run_claude(prompt, ["-p", "--output-format", "json"], tmp_path)
    call = fake.calls[0]
    assert call["input"] == prompt
    assert "含換行" not in " ".join(call["argv"])


def test_呼叫在指定的乾淨目錄執行(monkeypatch, tmp_path):
    fake = _FakeRun([_envelope("{}")])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    jc.run_claude("x", ["-p"], tmp_path)
    assert fake.calls[0]["cwd"] == str(tmp_path)


def test_非零離開會_raise_而且訊息不夾帶輸出(monkeypatch, tmp_path):
    fake = _FakeRun([_envelope("{}")], returncode=1)
    monkeypatch.setattr(jc.subprocess, "run", fake)
    with pytest.raises(ValueError) as exc:
        jc.run_claude("SECRET-PREFIX-DO-NOT-LEAK", ["-p"], tmp_path)
    assert "SECRET-PREFIX" not in str(exc.value)


# --- 信封解析 ---------------------------------------------------------------
def test_信封取得需要的欄位():
    got = jc.parse_envelope(_envelope('{"a": 1}'))
    assert got["result"] == '{"a": 1}'
    assert got["total_cost_usd"] == 0.0066
    assert got["input_tokens"] == 2
    assert got["output_tokens"] == 151
    assert got["thinking_tokens"] == 0
    assert got["duration_ms"] == 1660


@pytest.mark.parametrize("bad", ["", "   ", "not json", "[1,2]"])
def test_壞掉的信封會_raise(bad):
    with pytest.raises(ValueError):
        jc.parse_envelope(bad)


def test_is_error_為真會_raise():
    with pytest.raises(ValueError):
        jc.parse_envelope(_envelope("{}", is_error=True))


def test_subtype_不是_success_會_raise():
    with pytest.raises(ValueError):
        jc.parse_envelope(_envelope("{}", subtype="error_max_turns"))


def test_信封解析失敗的訊息不夾帶原文():
    secret = "SENSITIVE-ENVELOPE-PAYLOAD-0123456789"
    try:
        jc.parse_envelope("{" + secret)
    except ValueError as exc:
        assert secret not in str(exc)
    else:
        pytest.fail("應該要 raise")


def test_成本欄位缺失時回_None_而不是炸掉():
    env = _json.dumps({"type": "result", "subtype": "success",
                       "is_error": False, "result": "{}"})
    got = jc.parse_envelope(env)
    assert got["total_cost_usd"] is None
    assert got["input_tokens"] is None
    assert got["thinking_tokens"] is None


def test_成本缺值不算可疑():
    """成本缺值是另一件事，由欄位缺失的處理負責，不該誤報成盲化失效。"""
    assert jc.cost_is_suspicious(None) is False
    assert jc.cost_is_suspicious("") is False


# --- 盲化自我檢查 -----------------------------------------------------------
def test_盲化檢查回_NO_通過(monkeypatch, tmp_path):
    fake = _FakeRun([_envelope('{"answer": "NO"}')])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    got = jc.blindness_check(jc.cli_flags(jc.build_json_schema(FIELDS), 1.0),
                             tmp_path)
    assert got["answer"] == "NO"


def test_盲化檢查回_YES_就_SystemExit(monkeypatch, tmp_path):
    """判定器讀得到專案就等於預標籤污染，而那不會在輸出上留下痕跡。"""
    fake = _FakeRun([_envelope('{"answer": "YES", "kind": "專案說明"}')])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    with pytest.raises(SystemExit) as exc:
        jc.blindness_check(jc.cli_flags(jc.build_json_schema(FIELDS), 1.0),
                           tmp_path)
    assert "盲化" in str(exc.value)


@pytest.mark.parametrize("bad", ["不是 json", '{"answer": "MAYBE"}', "{}"])
def test_盲化檢查無法解析就_SystemExit(monkeypatch, tmp_path, bad):
    fake = _FakeRun([_envelope(bad)])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    with pytest.raises(SystemExit):
        jc.blindness_check(jc.cli_flags(jc.build_json_schema(FIELDS), 1.0),
                           tmp_path)


def test_盲化檢查用自己的_schema(monkeypatch, tmp_path):
    """探針要的是 YES/NO，不是三軸——用判定的 schema 問會逼它答錯格式。"""
    fake = _FakeRun([_envelope('{"answer": "NO"}')])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    jc.blindness_check(jc.cli_flags(jc.build_json_schema(FIELDS), 1.0), tmp_path)
    argv = fake.calls[0]["argv"]
    schema = _json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["answer"]["enum"] == ["YES", "NO"]
    assert "frame_owner" not in schema["properties"]


def test_盲化檢查沒有任何開關():
    """硬性的。CLI 不得有跳過它的參數，程式裡也不得有旁路。"""
    source = JUDGE.read_text(encoding="utf-8")
    code = source.split("from __future__ import annotations", 1)[1]
    for bad in ("--skip-blind", "--no-blind", "skip_blindness", "--unsafe"):
        assert bad not in code, bad
    assert "blindness_check(" in code


def test_每_25_群重跑一次():
    assert jc.BLIND_CHECK_EVERY == 25
    assert jc.should_recheck(1) is False        # 啟動時已經跑過
    assert jc.should_recheck(26) is True
    assert jc.should_recheck(51) is True
    for n in (2, 25, 27, 50, 52):
        assert jc.should_recheck(n) is False


# --- 成本門檻 ---------------------------------------------------------------
def test_成本超過門檻算可疑():
    assert jc.cost_is_suspicious(0.0686) is True    # 實測的預設設定成本
    assert jc.cost_is_suspicious(0.0066) is False   # 實測的盲化設定成本
    assert jc.cost_is_suspicious(jc.COST_WARN_USD) is False


def test_成本門檻只警示不中斷():
    """前綴長度差異本來就會讓成本浮動，門檻是事後看分布用的。"""
    source = JUDGE.read_text(encoding="utf-8")
    block = source.split("cost_is_suspicious(env", 1)[1].split("\n\n", 1)[0]
    assert "continue" not in block and "break" not in block
    assert "raise" not in block


# --- json-schema ------------------------------------------------------------
def test_schema_的_enum_與_FIELDS_逐項相同():
    """手寫第二份會跟 FIELDS 漂，而漂了之後 schema 仍然合法。"""
    carrier = jc._load_carrier()
    schema = jc.build_json_schema(carrier.FIELDS)
    for axis, values in carrier.FIELDS.items():
        assert schema["properties"][axis]["enum"] == list(values), axis
    assert schema["properties"]["confidence"]["enum"] == list(jc.CONFIDENCE_VALUES)
    assert set(schema["required"]) == set(carrier.FIELDS) | {"reason", "confidence"}
    assert schema["additionalProperties"] is False


def test_schema_跟著_FIELDS_變():
    a = jc.build_json_schema(FIELDS)
    b = jc.build_json_schema({**FIELDS, "frame_owner": ("tool", "user")})
    assert a != b


def test_reason_在_schema_裡沒有值域限制():
    """schema 管不了「不得引用前綴」，那條由 reason_leaks_prefix 擋。"""
    schema = jc.build_json_schema(FIELDS)
    assert "enum" not in schema["properties"]["reason"]


# --- 旗標與指紋 -------------------------------------------------------------
def test_旗標集合含所有盲化旗標():
    flags = jc.cli_flags(jc.build_json_schema(FIELDS), 4.5)
    for required in ("--safe-mode", "--system-prompt", "--tools",
                     "--strict-mcp-config", "--disable-slash-commands",
                     "--permission-prompts", "--no-session-persistence",
                     "--model", "--effort", "--json-schema",
                     "--max-budget-usd", "--output-format"):
        assert required in flags, required
    assert flags[flags.index("--tools") + 1] == ""          # 停用所有工具
    assert flags[flags.index("--model") + 1] == jc.MODEL
    assert flags[flags.index("--effort") + 1] == jc.EFFORT
    assert "--bare" not in flags                            # 它會強制 API 金鑰


@pytest.mark.parametrize("flag,value", [
    ("--safe-mode", None),
    ("--tools", "default"),
    ("--model", "claude-sonnet-5"),
    ("--effort", "high"),
    ("--system-prompt", "別的系統提示"),
])
def test_改任一旗標指紋會變(flag, value):
    """--safe-mode 拿掉、--tools 換成 default，都會讓判定條件變成另一回事，
    而那不會出現在提示詞模板裡。"""
    schema = jc.build_json_schema(FIELDS)
    base = jc.cli_flags(schema, 4.5)
    a = jc.template_fingerprint(_axes(), flags=base, schema=schema)
    changed = list(base)
    i = changed.index(flag)
    if value is None:
        del changed[i]
    else:
        changed[i + 1] = value
    b = jc.template_fingerprint(_axes(), flags=changed, schema=schema)
    assert a != b, f"{flag} 改了但指紋沒變"


def test_改_schema_指紋會變():
    flags = jc.cli_flags(jc.build_json_schema(FIELDS), 4.5)
    a = jc.template_fingerprint(_axes(), flags=flags,
                                schema=jc.build_json_schema(FIELDS))
    b = jc.template_fingerprint(_axes(), flags=flags, schema={"type": "object"})
    assert a != b


def test_改執行路徑指紋會變():
    assert (jc.template_fingerprint(_axes(), runtime="claude_code")
            != jc.template_fingerprint(_axes(), runtime="api"))


def test_預算不進指紋():
    """--max-budget-usd 是保險絲不是判定條件：只是把上限調高，
    不該看起來像換了一套判定條件。"""
    schema = jc.build_json_schema(FIELDS)
    a = jc.template_fingerprint(_axes(), flags=jc.cli_flags(schema, 1.0),
                                schema=schema)
    b = jc.template_fingerprint(_axes(), flags=jc.cli_flags(schema, 99.0),
                                schema=schema)
    assert a == b
    # 但旗標名本身要在——拿掉它是真的改變了行為
    assert "--max-budget-usd" in jc.fingerprint_flags(jc.cli_flags(schema, 1.0))


def test_逐次上限是保險絲不是預算():
    assert jc.PER_CALL_BUDGET_USD > jc.MEASURED_PER_CALL_USD * 3


# --- 退避重試 ---------------------------------------------------------------
def test_失敗會退避重試(monkeypatch, tmp_path):
    fake = _FakeRun(["", "", _envelope('{"a":1}')])   # 前兩次空輸出 → ValueError
    monkeypatch.setattr(jc.subprocess, "run", fake)
    monkeypatch.setattr(jc.time, "sleep", lambda s: None)
    got = jc.call_with_backoff("x", ["-p"], tmp_path)
    assert got["result"] == '{"a":1}'
    assert len(fake.calls) == 3


def test_重試耗盡會_raise_而不是中斷整批(monkeypatch, tmp_path):
    fake = _FakeRun([""] * (jc.MAX_RETRIES + 1))
    monkeypatch.setattr(jc.subprocess, "run", fake)
    monkeypatch.setattr(jc.time, "sleep", lambda s: None)
    with pytest.raises(ValueError):
        jc.call_with_backoff("x", ["-p"], tmp_path)
    assert len(fake.calls) == jc.MAX_RETRIES + 1


def test_重試耗盡的訊息不夾帶提示詞(monkeypatch, tmp_path):
    fake = _FakeRun([""] * (jc.MAX_RETRIES + 1))
    monkeypatch.setattr(jc.subprocess, "run", fake)
    monkeypatch.setattr(jc.time, "sleep", lambda s: None)
    with pytest.raises(ValueError) as exc:
        jc.call_with_backoff("SECRET-PREFIX-XYZ", ["-p"], tmp_path)
    assert "SECRET-PREFIX" not in str(exc.value)


# --- 探針問法與指紋 ---------------------------------------------------------
# 探針問法變了等於檢查強度變了。放寬問法會讓判定照樣通過、指紋不變、
# 輸出檔看起來一模一樣——而那正是「這批結果是在什麼條件下產生的」這個
# 問題最需要答案的地方。
def test_探針問四樣具體的東西_不問上下文總量():
    """第一版問「除了本則訊息之外還有沒有任何其他內容」，而我們自己的
    --system-prompt 按定義就是「其他內容」，於是必然回 YES、整批必然被擋。
    太嚴是安全的方向，但一個必然觸發的檢查最後一定會被關掉。"""
    p = jc.BLIND_PROBE_PROMPT
    for item in ("專案說明文件", "CLAUDE.md", "記憶", "先前的對話", "檔案系統"):
        assert item in p, item
    assert "除了本則訊息之外" not in p
    assert "任何其他內容" not in p


def test_改探針一個字指紋就變():
    axes = _axes()
    flags = jc.cli_flags(jc.build_json_schema(FIELDS), 4.5)
    schema = jc.build_json_schema(FIELDS)
    before = jc.template_fingerprint(axes, flags=flags, schema=schema)
    original = jc.BLIND_PROBE_PROMPT
    try:
        jc.BLIND_PROBE_PROMPT = original + "。"      # 多一個句號
        after = jc.template_fingerprint(axes, flags=flags, schema=schema)
    finally:
        jc.BLIND_PROBE_PROMPT = original
    assert before != after, "探針問法改了但指紋沒變"


def test_改探針值域指紋也會變():
    """把 YES/NO 放寬成 YES/NO/MAYBE 就是放寬了檢查強度。"""
    axes = _axes()
    flags = jc.cli_flags(jc.build_json_schema(FIELDS), 4.5)
    schema = jc.build_json_schema(FIELDS)
    before = jc.template_fingerprint(axes, flags=flags, schema=schema)
    original = jc.BLIND_PROBE_SCHEMA
    try:
        jc.BLIND_PROBE_SCHEMA = {**original, "properties": {
            **original["properties"],
            "answer": {"type": "string", "enum": ["YES", "NO", "MAYBE"]}}}
        after = jc.template_fingerprint(axes, flags=flags, schema=schema)
    finally:
        jc.BLIND_PROBE_SCHEMA = original
    assert before != after


def test_探針回_NO_才繼續(monkeypatch, tmp_path):
    fake = _FakeRun([_envelope('{"answer": "NO", "kind": "none"}')])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    got = jc.blindness_check(jc.cli_flags(jc.build_json_schema(FIELDS), 1.0),
                             tmp_path)
    assert got["answer"] == "NO"


@pytest.mark.parametrize("kind", ["專案說明文件", "記憶", "先前的對話", "檔案系統工具"])
def test_探針回_YES_仍然擋得住(monkeypatch, tmp_path, kind):
    """放寬問法之後，四項裡任何一項出現都還是要擋。"""
    fake = _FakeRun([_envelope(
        _json.dumps({"answer": "YES", "kind": kind}, ensure_ascii=False))])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    with pytest.raises(SystemExit):
        jc.blindness_check(jc.cli_flags(jc.build_json_schema(FIELDS), 1.0),
                           tmp_path)


# --- 啟動檢查要走實際執行的那條路徑 -----------------------------------------
def test_resolve_claude_走完整路徑而不是裸名字():
    """Windows 上 npm 裝的是 claude.CMD，另有一個沒有副檔名的 shim。
    subprocess.run 不帶 shell 走 CreateProcess，它不查 PATHEXT，於是撿到
    那個 shim 卻執行不了。shutil.which 會查 PATHEXT。"""
    source = JUDGE.read_text(encoding="utf-8")
    code = source.split("from __future__ import annotations", 1)[1]
    assert "[resolve_claude(), *flags]" in code
    assert "[CLAUDE_BIN, *flags]" not in code


def test_resolve_claude_用_which(monkeypatch):
    monkeypatch.setattr(jc, "_CLAUDE_EXE", None)
    monkeypatch.setattr(jc.shutil, "which", lambda name: r"C:\x\claude.CMD")
    assert jc.resolve_claude() == r"C:\x\claude.CMD"


def test_resolve_claude_找不到時退回裸名字(monkeypatch):
    """找不到就退回裸名字讓 subprocess 自己報錯，不要靜默用一個空字串。"""
    monkeypatch.setattr(jc, "_CLAUDE_EXE", None)
    monkeypatch.setattr(jc.shutil, "which", lambda name: None)
    assert jc.resolve_claude() == jc.CLAUDE_BIN


def test_呼叫用的是解析後的路徑(monkeypatch, tmp_path):
    monkeypatch.setattr(jc, "_CLAUDE_EXE", None)
    monkeypatch.setattr(jc.shutil, "which", lambda name: r"C:\x\claude.CMD")
    fake = _FakeRun([_envelope("{}")])
    monkeypatch.setattr(jc.subprocess, "run", fake)
    jc.run_claude("x", ["-p"], tmp_path)
    assert fake.calls[0]["argv"][0] == r"C:\x\claude.CMD"


# --- except 要涵蓋 SystemExit 與 Exception 兩者 -----------------------------
def test_週期檢查的_except_涵蓋兩種例外():
    """SystemExit 不是 Exception 的子類。只寫 except SystemExit 的話，
    其餘例外全部漏過去帶著 traceback 跑，而那條路徑的區域變數含提示詞
    與前綴。見 ENGINEERING_NOTES〈明文邊界要涵蓋例外訊息〉。"""
    source = JUDGE.read_text(encoding="utf-8")
    code = source.split("from __future__ import annotations", 1)[1]
    assert "except (SystemExit, Exception) as exc:" in code
    # 不得有只擋 SystemExit 的裸子句
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("except SystemExit"):
            assert stripped == "except SystemExit:", stripped   # 只允許 re-raise


def test_啟動檢查的例外不會_raise_到頂層():
    source = JUDGE.read_text(encoding="utf-8")
    block = source.split("盲化自我檢查（啟動）", 1)[1].split('print("NO ✓")', 1)[0]
    assert "except Exception as exc:" in block
    assert "raise SystemExit(" in block
    # 只印型別名，不印例外訊息
    assert "{type(exc).__name__}" in block
    assert "{exc}" not in block


def test_週期檢查失敗時只印型別名():
    source = JUDGE.read_text(encoding="utf-8")
    block = source.split("盲化自我檢查（第 {n} 群之前）", 1)[1].split("break", 1)[0]
    assert "isinstance(exc, SystemExit)" in block
    assert "type(exc).__name__" in block


# ===========================================================================
# 預算：兩個作用域
#
# 這一組是〈保險絲的單位要跟被保護的東西同單位〉的迴歸測試。舊版把群數
# 乘進 `--max-budget-usd`，於是同一個數字在兩頭都錯而且方向相反：
# n=1 算出 0.02（低於實際單次 0.0276，最後一群被自己擋掉）、
# n=225 算出 4.46（是單次的 160 倍，形同不存在）。
# **所以下面每一條都要指名它驗的是哪一個作用域。**
# ===========================================================================
def test_逐次上限不隨群數變動():
    """舊 bug 的根源：上限的單位是「一次呼叫」，群數不該出現在裡面。"""
    schema = jc.build_json_schema(FIELDS)
    flags = jc.cli_flags(schema)
    assert flags[flags.index("--max-budget-usd") + 1] == \
        f"{jc.PER_CALL_BUDGET_USD:.2f}"
    # 而且函式簽名裡沒有群數可以傳進來
    import inspect
    assert "n_groups" not in inspect.signature(jc.cli_flags).parameters


def test_只剩一群時上限仍然高於實測單次():
    """**作用域的最小情況。** 舊版 budget_usd(1)=0.02 < 實測 0.0276——
    續跑到最後一群時，那一群會被自己的保險絲擋掉，看起來卻像模型拒答。"""
    schema = jc.build_json_schema(FIELDS)
    for n_todo in (1, 2, 5, 225):
        flags = jc.cli_flags(schema)      # 不吃群數，所以 n_todo 動不了它
        budget = float(flags[flags.index("--max-budget-usd") + 1])
        # 要蓋過**最差的那一次**，不是平均。實測最高的 B044 是 0.0454，
        # 成因是那次呼叫在內部跑了兩輪；用平均訂上限會把它擋掉。
        assert budget > jc.MEASURED_PER_CALL_MAX_USD, n_todo


def test_整批上限與逐次上限是兩個數字():
    assert jc.PER_CALL_BUDGET_USD != jc.BATCH_BUDGET_USD
    assert jc.BATCH_BUDGET_USD > jc.PER_CALL_BUDGET_USD


def test_逐次上限乘上群數會超過整批上限():
    """**作用域的最大情況**，也是整批上限存在的理由：225 次各自守住逐次
    上限，加起來仍然可以是整批上限的兩倍以上。旗標擋不到這件事。"""
    assert 225 * jc.PER_CALL_BUDGET_USD > jc.BATCH_BUDGET_USD


def test_整批上限高於實際估計值():
    """225 群的估計 = 225 次判定 + 9 次盲化檢查。"""
    estimate = 225 * jc.MEASURED_PER_CALL_USD + 9 * jc.MEASURED_PROBE_USD
    assert jc.BATCH_BUDGET_USD > estimate * 2


def test_整批累加到超過才停():
    b = jc.BatchBudget(cap_usd=0.10)
    for _ in range(3):
        b.spend(0.03)
    assert b.spent == pytest.approx(0.09)
    assert b.exceeded() is False


def test_整批剛好等於上限不停():
    """上限是「不准超過」不是「不准達到」。反過來會少判一群而且沒有徵兆。"""
    b = jc.BatchBudget(cap_usd=0.10)
    b.spend(0.10)
    assert b.exceeded() is False
    b.spend(0.0001)
    assert b.exceeded() is True


def test_整批記得停在哪一群():
    b = jc.BatchBudget(cap_usd=0.05)
    b.spend(0.06)
    assert b.exceeded() is True
    b.stop("A042")
    assert b.stopped_at == "A042"


def test_整批一開始沒有停在任何地方():
    assert jc.BatchBudget().stopped_at is None


def test_成本缺值另外計數而不是當成零():
    """缺值當 0 累加，整批上限就悄悄失效了——收尾必須看得出來。"""
    b = jc.BatchBudget(cap_usd=1.0)
    b.spend(None)
    b.spend("壞掉的值")
    b.spend(0.02)
    assert b.unknown == 2
    assert b.spent == pytest.approx(0.02)


def test_探針與判定的成本都進整批記帳():
    """盲化檢查也花錢。只算判定會讓整批上限比它宣稱的寬。"""
    source = JUDGE.read_text(encoding="utf-8")
    body = source.split("def main(", 1)[1]
    assert body.count("budget.spend(probe.get") == 2      # 啟動 + 每 25 群
    assert "budget.spend(env.get" in body


def test_整批上限的檢查在迴圈裡而且會停下來():
    """`--max-budget-usd` 管不到整批，所以這件事只能由迴圈自己做。"""
    source = JUDGE.read_text(encoding="utf-8")
    body = source.split("for n, gid in enumerate(todo, 1):", 1)[1]
    block = body.split("budget.exceeded()", 1)[1].split("\n\n", 1)[0]
    assert "budget.stop(gid)" in block
    assert "break" in block


def test_不再有把群數乘進上限的函式():
    """名字對不上語義的函式留著，下一個人會再用它一次。"""
    source = JUDGE.read_text(encoding="utf-8")
    for gone in ("budget_usd", "BUDGET_PER_GROUP_USD", "BUDGET_SAFETY_FACTOR"):
        assert gone not in source, gone


# --- 成本門檻的重訂 ---------------------------------------------------------
def test_門檻落在實測盲化值與實測預設值之間():
    """門檻的用途是分辨這兩件事，所以它必須在兩者之間。"""
    assert jc.MEASURED_PER_CALL_USD < jc.COST_WARN_USD
    assert jc.COST_WARN_USD < jc.MEASURED_DEFAULT_CALL_USD


def test_實測最貴的那群不該被判為可疑():
    """實測最高 0.0454（B044，內部兩輪）。門檻壓在它底下就會每跑幾群叫一次，
    而一個常態誤報的檢查，最後一定會被關掉。"""
    assert jc.cost_is_suspicious(jc.MEASURED_PER_CALL_MAX_USD) is False
    assert jc.cost_is_suspicious(jc.MEASURED_DEFAULT_CALL_USD) is True


def test_門檻與實測值的關係():
    """未校準時要留大餘裕（寧可不叫）；校準後要蓋過實測最高值並留一成。"""
    assert isinstance(jc.COST_WARN_CALIBRATED, bool)
    if jc.COST_WARN_CALIBRATED:
        assert jc.COST_WARN_USD >= jc.MEASURED_PER_CALL_MAX_USD * 1.1
    else:
        assert jc.COST_WARN_USD >= jc.MEASURED_PER_CALL_MAX_USD * 1.5


def test_門檻與未盲化實測值之間的餘裕要記在案():
    """0.06 與 0.0686 只差 14%——這條防線很薄，薄到必須有人知道它薄。
    真的越過門檻時，要先看 prompt_tokens_total 而不是直接下結論。"""
    margin = jc.MEASURED_DEFAULT_CALL_USD / jc.COST_WARN_USD
    assert 1.0 < margin < 1.5
    source = JUDGE.read_text(encoding="utf-8")
    assert "prompt_tokens_total` 才是" in source


def test_未校準時抬頭要說出來():
    """一個沒有標記的暫定值，三個月後就是一個看起來量過的值。"""
    source = JUDGE.read_text(encoding="utf-8")
    assert "COST_WARN_CALIBRATED" in source.split("def main(", 1)[1]


# --- token 三欄 -------------------------------------------------------------
def test_信封取得快取兩欄與總和():
    got = jc.parse_envelope(_envelope("{}"))
    assert got["cache_creation_input_tokens"] == 812
    assert got["cache_read_input_tokens"] == 0
    assert got["prompt_tokens_total"] == 814


def test_input_tokens_單獨看會嚴重低估():
    """v1 那五列全是 2。那不是「提示詞只有 2 個 token」。"""
    got = jc.parse_envelope(_envelope("{}"))
    assert got["input_tokens"] == 2
    assert got["prompt_tokens_total"] > got["input_tokens"] * 100


def test_三者相加():
    assert jc.prompt_tokens_total(2, 812, 0) == 814
    assert jc.prompt_tokens_total(0, 0, 0) == 0
    assert jc.prompt_tokens_total(10, 20, 30) == 60


@pytest.mark.parametrize("parts", [(None, 812, 0), (2, None, 0), (2, 812, None),
                                   (None, None, None)])
def test_缺任一欄回_None_而不是用零補(parts):
    """0 補出來的總數看起來合理、不會報錯、而且必定偏小。"""
    assert jc.prompt_tokens_total(*parts) is None


def test_非數字回_None():
    assert jc.prompt_tokens_total(2, "x", 0) is None


def test_信封沒有_usage_時三欄都是_None():
    env = _json.dumps({"type": "result", "subtype": "success",
                       "is_error": False, "result": "{}"})
    got = jc.parse_envelope(env)
    assert got["cache_creation_input_tokens"] is None
    assert got["cache_read_input_tokens"] is None
    assert got["prompt_tokens_total"] is None


def test_快取欄位改名時會變成整欄空值而不是偏小的數():
    """欄位改名是看得見的失效；補 0 是看不見的失效。"""
    env = _json.loads(_envelope("{}"))
    env["usage"]["cache_creation_tokens"] = \
        env["usage"].pop("cache_creation_input_tokens")
    got = jc.parse_envelope(_json.dumps(env))
    assert got["prompt_tokens_total"] is None


# --- 輸出格式版本與遷移 -----------------------------------------------------
def test_v2_欄位含三個_token_欄位與版本欄():
    for col in ("cache_creation_input_tokens", "cache_read_input_tokens",
                "prompt_tokens_total", "schema_version"):
        assert col in jc.OUTPUT_COLUMNS, col
    jc.check_output_columns(jc.OUTPUT_COLUMNS)


def test_v1_是_v2_的前綴():
    """新欄位一律往後加。插在中間會讓遷移前後的欄位對不起來。"""
    assert list(jc.OUTPUT_COLUMNS[:len(jc.OUTPUT_COLUMNS_V1)]) == \
        list(jc.OUTPUT_COLUMNS_V1)


def test_升級後舊列的三欄留空(tmp_path):
    path = tmp_path / "o.csv"
    _write_v1(path, [ROW_V1])
    assert jc.migrate_output(path) == 1
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == list(jc.OUTPUT_COLUMNS)
    for col in ("cache_creation_input_tokens", "cache_read_input_tokens",
                "prompt_tokens_total"):
        assert rows[0][col] == "", col


def test_升級不回填猜測值(tmp_path):
    """**不是 0，也不是 input_tokens 的複製。** 兩者都會讓舊列看起來像量過
    的列，而這個檔案的用途正是分辨這件事。"""
    path = tmp_path / "o.csv"
    _write_v1(path, [ROW_V1])
    jc.migrate_output(path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    assert row["prompt_tokens_total"] not in ("0", "2")
    assert row["input_tokens"] == "2"          # 原本量到的值不動


def test_升級後舊列標記為_v1(tmp_path):
    path = tmp_path / "o.csv"
    _write_v1(path, [ROW_V1, {**ROW_V1, "group_id": "A002"}])
    assert jc.migrate_output(path) == 2
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["schema_version"] for r in rows] == ["v1", "v1"]


def test_升級後新寫的列是_v2_而且看得出哪列是哪版(tmp_path):
    path = tmp_path / "o.csv"
    _write_v1(path, [ROW_V1])
    jc.migrate_output(path)
    jc.append_output(path, {**ROW, "group_id": "A002"})
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["schema_version"] for r in rows] == ["v1", "v2"]
    assert rows[0]["prompt_tokens_total"] == ""
    assert rows[1]["prompt_tokens_total"] == "814"


def test_升級是冪等的(tmp_path):
    path = tmp_path / "o.csv"
    _write_v1(path, [ROW_V1])
    jc.migrate_output(path)
    before = path.read_bytes()
    assert jc.migrate_output(path) == 0
    assert path.read_bytes() == before


def test_沒有檔案時不需要升級(tmp_path):
    assert jc.migrate_output(tmp_path / "nope.csv") == 0


def test_不認得的表頭不動它而是報錯(tmp_path):
    path = tmp_path / "o.csv"
    path.write_text("group_id,某個沒看過的欄\nA001,x\n", encoding="utf-8-sig")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        jc.migrate_output(path)
    assert path.read_bytes() == before


def test_舊表頭時_append_會報錯而不是錯位寫入(tmp_path):
    """**這一條是重點。** DictWriter 不看既有表頭，它照 fieldnames 寫：
    表頭 14 欄而每列 18 格，檔案仍然是合法的 csv，讀出來每一欄都錯位，
    而且沒有任何徵兆。"""
    path = tmp_path / "o.csv"
    _write_v1(path, [ROW_V1])
    with pytest.raises(ValueError) as exc:
        jc.append_output(path, dict(ROW))
    assert "migrate_output" in str(exc.value)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1                       # 沒有被寫進去


def test_升級後續跑仍然讀得到已判的群(tmp_path):
    path = tmp_path / "o.csv"
    _write_v1(path, [ROW_V1, {**ROW_V1, "group_id": "A002"}])
    jc.migrate_output(path)
    assert jc.load_done(path) == {"A001", "A002"}


# --- --only 不繞過閘門 ------------------------------------------------------
def test_only_只會讓待判變短():
    todo, refused = jc.apply_only(["A001", "A002", "A003"], "A002,A003")
    assert todo == ["A002", "A003"]
    assert refused == []


def test_only_指名沒通過閘門的群一樣不送():
    """**最重要的一條。** --only 是交集不是聯集：閘門在它之前，
    它不能把任何一個沒通過篩檢的群加回來。"""
    todo, refused = jc.apply_only(["A001"], "A001,A002,B999")
    assert todo == ["A001"]
    assert refused == ["A002", "B999"]


def test_only_指名的群全部沒通過時待判是空的():
    todo, refused = jc.apply_only(["A001"], "FLAGGED-1,FLAGGED-2")
    assert todo == []
    assert refused == ["FLAGGED-1", "FLAGGED-2"]


def test_沒給_only_時原樣不動():
    todo, refused = jc.apply_only(["A001", "A002"], None)
    assert todo == ["A001", "A002"]
    assert refused == []


@pytest.mark.parametrize("junk", ["", "  ", ",,", " , "])
def test_only_給了但解不出任何群就什麼都不送(junk):
    """**方向要對，判準是 None 不是 falsy。** 打錯的 --only 解成「全部」
    會送出 225 群，解成「零群」只是白跑一趟。`--only ""`（shell 變數是空的）
    在 falsy 判準下正好落進前者。寧可漏送不可誤送，跟閘門同一個方向。"""
    todo, refused = jc.apply_only(["A001", "A002"], junk)
    assert todo == []


def test_only_容忍空白與尾逗號():
    todo, refused = jc.apply_only(["A001", "A002"], " A001 , A002 ,")
    assert todo == ["A001", "A002"]
    assert refused == []


def test_only_被拒絕的群要說出來而不是安靜少送():
    """安靜地少送幾群，看起來會像那幾群本來就不在名單裡。"""
    source = JUDGE.read_text(encoding="utf-8")
    body = source.split("def main(", 1)[1]
    assert "refused" in body and "不送" in body


def test_only_在閘門之後才套用():
    source = JUDGE.read_text(encoding="utf-8")
    body = source.split("def main(", 1)[1]
    gate = body.index("todo = [g for g in all_ids if g in clean")
    assert gate < body.index("apply_only(todo"), "--only 必須在閘門之後套用"


# ===========================================================================
# 整批上限：真的跑一次迴圈
#
# 上面那條 test_整批上限的檢查在迴圈裡而且會停下來 是比對來源字串的，而
# 來源比對驗不了「這條路徑真的會被走到」——把它改成 `if False and
# budget.exceeded():` 之後，整組測試照樣全過。突變測試就是這樣抓到的。
# 見 ENGINEERING_NOTES〈測試可以驗一個永遠不會被執行的路徑〉。
#
# 所以這一組真的呼叫 main()：subprocess 全程 mock（**不呼叫 claude**），
# 資料檔是現造的假資料，判定結果的內容不重要，重要的是迴圈停在哪裡。
# ===========================================================================
def _fake_dataset(tmp_path, group_ids, screen_value="clean"):
    """造一組最小的資料檔，讓 main() 跑得起來。內容全部是現編的。"""
    meta = pd.DataFrame([{
        "group_id": g, "uid數": 3, "相異內容數": 40, "請求數": 120,
        "前綴佔中位內容長度比例": 0.4, "unit_type分布": "{}",
        "request_style分布": "{}", "內容長度_中位": 800,
    } for g in group_ids])
    meta.to_parquet(tmp_path / "clusters.parquet")
    pd.DataFrame([{
        "group_id": g, "prefix_raw": f"FRAME-{g}-" + "x" * 40,
        "prefix_normalized": f"FRAME-{g}-" + "x" * 40,
        "prefix_len_raw": 50, "prefix_len_normalized": 50, "truncated": False,
    } for g in group_ids]).to_parquet(tmp_path / "prefixes.parquet")
    pd.DataFrame([{"group_id": g, "personal_data": screen_value}
                  for g in group_ids]).to_csv(
        tmp_path / "screen.csv", index=False, encoding="utf-8-sig")
    return (tmp_path / "prefixes.parquet", tmp_path / "clusters.parquet",
            tmp_path / "screen.csv", tmp_path / "out.csv")


def _wire(monkeypatch, tmp_path, group_ids, envelopes, screen_value="clean"):
    prefixes, clusters, screen, out = _fake_dataset(
        tmp_path, group_ids, screen_value)
    monkeypatch.setattr(jc, "PREFIXES", prefixes)
    monkeypatch.setattr(jc, "CLUSTERS", clusters)
    monkeypatch.setattr(jc, "SCREEN_CSV", screen)
    monkeypatch.setattr(jc, "OUTPUT_CSV", out)
    monkeypatch.setattr(jc.shutil, "which", lambda name: "C:/fake/claude.CMD")
    fake = _FakeRun(envelopes)
    monkeypatch.setattr(jc.subprocess, "run", fake)
    return fake, out


def _real_fingerprint():
    """main() 會算出來的那個指紋。舊格式的 fixture 要帶著它，
    否則會先被指紋守門擋掉——那道守門在遷移之前，而且順序是對的。"""
    carrier = jc._load_carrier()
    schema = jc.build_json_schema(carrier.FIELDS)
    axes = jc.render_axes(carrier.FIELDS, carrier.FIELD_HELP)
    return jc.template_fingerprint(axes, "raw", runtime="claude_code",
                                   flags=jc.cli_flags(schema), schema=schema)


def _judgement(cost):
    """一筆合法的判定回覆。三軸的值取值域裡的第一個，內容不重要。"""
    body = _json.dumps({"frame_owner": "tool", "disposition": "keep",
                        "axis_level": "none", "reason": "結構描述",
                        "confidence": "medium"}, ensure_ascii=False)
    return _envelope(body, total_cost_usd=cost)


def _probe():
    return _envelope(_json.dumps({"answer": "NO", "kind": ""}))


def test_整批上限用完就停在那一群(monkeypatch, tmp_path, capsys):
    """**真的跑迴圈。** 每群 20 USD、上限 15：第一群判完就該停，
    後面兩群完全不呼叫。"""
    ids = ["A001", "A002", "A003"]
    fake, out = _wire(monkeypatch, tmp_path, ids,
                      [_probe(), _judgement(20.0), _judgement(20.0),
                       _judgement(20.0)])
    assert jc.main([]) == 0
    printed = capsys.readouterr().out
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["group_id"] for r in rows] == ["A001"]     # 只判了一群
    assert len(fake.calls) == 2                          # 探針 + 一次判定
    assert "整批上限" in printed and "停在 A001" in printed


def test_沒超過上限就把全部判完(monkeypatch, tmp_path):
    """對照組。沒有它，上一條在「永遠停在第一群」時也會過。"""
    ids = ["A001", "A002", "A003"]
    fake, out = _wire(monkeypatch, tmp_path, ids,
                      [_probe()] + [_judgement(0.0276)] * 3)
    assert jc.main([]) == 0
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["group_id"] for r in rows] == ids
    assert len(fake.calls) == 4


def test_停下來時已判的那一群留著(monkeypatch, tmp_path):
    """花掉的錢收不回來，那一群的結果是有效的。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001", "A002"],
                      [_probe(), _judgement(20.0), _judgement(20.0)])
    jc.main([])
    assert jc.load_done(out) == {"A001"}


def test_停下來之後續跑會接著判(monkeypatch, tmp_path):
    """整批上限是每次執行各自算的，續跑不會被上一次的累計卡住。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001", "A002"],
                      [_probe(), _judgement(20.0), _judgement(20.0)])
    jc.main([])
    fake2, out2 = _wire(monkeypatch, tmp_path, ["A001", "A002"],
                        [_probe(), _judgement(20.0)])
    assert out2 == out
    jc.main([])
    assert jc.load_done(out) == {"A001", "A002"}


def test_探針的成本也算進整批(monkeypatch, tmp_path):
    """探針一次 0.0184，225 群要跑 9 次。只算判定會讓上限比宣稱的寬。"""
    probe_env = _envelope(_json.dumps({"answer": "NO", "kind": ""}),
                          total_cost_usd=20.0)
    fake, out = _wire(monkeypatch, tmp_path, ["A001", "A002"],
                      [probe_env, _judgement(0.01), _judgement(0.01)])
    jc.main([])
    # 探針就已經把上限用掉了，所以判完第一群就停
    assert jc.load_done(out) == {"A001"}


def test_寫出來的列是_v2_而且三個_token_欄位有值(monkeypatch, tmp_path):
    fake, out = _wire(monkeypatch, tmp_path, ["A001"],
                      [_probe(), _judgement(0.0276)])
    jc.main([])
    with out.open(encoding="utf-8-sig", newline="") as fh:
        row = next(csv.DictReader(fh))
    assert row["schema_version"] == "v2"
    assert row["input_tokens"] == "2"
    assert row["cache_creation_input_tokens"] == "812"
    assert row["prompt_tokens_total"] == "814"


def test_舊檔在判定之前就被升級(monkeypatch, tmp_path):
    """升級發生在第一次 append 之前。晚一步就是整個檔案錯位。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001", "A002"],
                      [_probe(), _judgement(0.0276)])
    _write_v1(out, [{**ROW_V1, "group_id": "A002",
                     "prompt_sha256": _real_fingerprint()}])
    assert jc.main([]) == 0
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["group_id"] for r in rows] == ["A002", "A001"]
    assert [r["schema_version"] for r in rows] == ["v1", "v2"]
    assert rows[0]["prompt_tokens_total"] == ""       # 舊列不回填
    assert rows[1]["prompt_tokens_total"] == "814"


def test_flagged_的群連迴圈都進不去(monkeypatch, tmp_path):
    """閘門的端到端驗證：整批跑完一次呼叫都沒有發生。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001", "A002"],
                      [_probe()], screen_value="flagged")
    assert jc.main([]) == 0
    assert len(fake.calls) == 0                       # 連盲化探針都不用跑
    assert not out.exists()


# ===========================================================================
# 指紋守門
#
# 每一列都寫了 prompt_sha256，但在這之前沒有任何東西讀它——**寫下來不等於
# 被檢查**。指紋換掉之後 load_done 照樣把舊列當成「已判」，於是新條件一列
# 都不會產生，而收尾會說「沒有待判的群，結束」，看起來像跑完了。
# 實際發生過一次：盲化探針的問法改了，107 列變成不可比。
# ===========================================================================
def test_讀得出輸出檔裡的指紋(tmp_path):
    path = tmp_path / "o.csv"
    jc.append_output(path, {**ROW, "prompt_sha256": "a" * 64})
    jc.append_output(path, {**ROW, "group_id": "A002", "prompt_sha256": "b" * 64})
    assert jc.output_fingerprints(path) == {"a" * 64, "b" * 64}


def test_沒有檔案時沒有指紋(tmp_path):
    assert jc.output_fingerprints(tmp_path / "nope.csv") == set()
    assert jc.stale_fingerprints(tmp_path / "nope.csv", "a" * 64) == set()


def test_同一個指紋不算過期(tmp_path):
    path = tmp_path / "o.csv"
    jc.append_output(path, {**ROW, "prompt_sha256": "a" * 64})
    assert jc.stale_fingerprints(path, "a" * 64) == set()


def test_不同指紋算過期(tmp_path):
    path = tmp_path / "o.csv"
    jc.append_output(path, {**ROW, "prompt_sha256": "a" * 64})
    assert jc.stale_fingerprints(path, "b" * 64) == {"a" * 64}


def test_混著兩個指紋時兩個都要報(tmp_path):
    """只報一個會讓人以為改名一次就好。"""
    path = tmp_path / "o.csv"
    jc.append_output(path, {**ROW, "prompt_sha256": "a" * 64})
    jc.append_output(path, {**ROW, "group_id": "A002", "prompt_sha256": "b" * 64})
    assert jc.stale_fingerprints(path, "c" * 64) == {"a" * 64, "b" * 64}


def test_指紋不同時_main_停下來而不是靜靜跳過(monkeypatch, tmp_path):
    """**這一條是重點。** 不擋的話 todo 會是空的，然後印「沒有待判的群，
    結束」——與真的跑完了一模一樣。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001", "A002"],
                      [_probe(), _judgement(0.03)])
    jc.append_output(out, {**ROW, "group_id": "A001",
                           "prompt_sha256": "z" * 64})
    with pytest.raises(SystemExit) as exc:
        jc.main([])
    assert "不可比" in str(exc.value)
    assert len(fake.calls) == 0          # 一次呼叫都沒發生


def test_指紋守門在_dry_run_也會擋(monkeypatch, tmp_path):
    """要在花錢之前就知道。dry-run 過了才跑，是這支程式的用法。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001"], [])
    jc.append_output(out, {**ROW, "prompt_sha256": "z" * 64})
    with pytest.raises(SystemExit):
        jc.main(["--dry-run"])


def test_指紋守門的訊息要說出舊指紋(monkeypatch, tmp_path):
    """改名要帶上舊指紋前 8 碼，訊息不說就得自己去翻檔案。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001"], [])
    jc.append_output(out, {**ROW, "prompt_sha256": "z" * 64})
    with pytest.raises(SystemExit) as exc:
        jc.main(["--dry-run"])
    assert "z" * 16 in str(exc.value)


# --- 盲化探針的豁免 ---------------------------------------------------------
def test_探針的豁免只放行環境資訊():
    """docstring 早就寫著「Claude Code 注入的環境 context」是刻意不問的，
    但那個豁免只在 docstring 裡，探針的第 (2) 項字面上仍然涵蓋它——
    於是它對一個恆真條件時而回 NO 時而回 YES（實測 16 次 15 NO / 1 YES）。
    **豁免要寫在被執行的那份文字裡，不是寫在旁邊的說明裡。**"""
    probe = jc.BLIND_PROBE_PROMPT
    assert "例外" in probe
    assert "當前日期" in probe
    # 豁免必須是窄的：除此之外的都算
    assert "除此之外的任何記憶都算" in probe
    # 真正危險的三項一項都不能被放行
    for must in ("CLAUDE.md", "先前的對話紀錄", "讀取檔案系統的工具"):
        assert must in probe, must


def test_探針問法改了指紋就會變():
    """放寬問法而指紋不變，是這支程式最不能出的錯之一。"""
    a = jc.template_fingerprint(_axes())
    original = jc.BLIND_PROBE_PROMPT
    try:
        jc.BLIND_PROBE_PROMPT = original + "（放寬）"
        b = jc.template_fingerprint(_axes())
    finally:
        jc.BLIND_PROBE_PROMPT = original
    assert a != b


def test_舊格式但指紋也不同時先擋指紋(monkeypatch, tmp_path):
    """兩個問題同時存在時，先報不可比的那個——遷移一個不可比的檔案
    只會讓它看起來更像可以續跑。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001", "A002"], [])
    _write_v1(out, [{**ROW_V1, "group_id": "A002",
                     "prompt_sha256": "z" * 64}])
    with pytest.raises(SystemExit) as exc:
        jc.main(["--dry-run"])
    assert "不可比" in str(exc.value)
    # 而且沒有被遷移
    assert jc.read_header(out) == list(jc.OUTPUT_COLUMNS_V1)


# --- OutputGuard：明文不得寫到螢幕上 ----------------------------------------
#
# 這是檔頭第 (2)(3) 條的測試。那兩條原本只由「原始碼裡剛好沒有那幾行
# print」成立，而那是一個性質不是一個保證——所以這裡測的是守門本身，
# 不是「目前有沒有人 print」。

SECRET = "病患主訴胸悶合併呼吸困難已持續三日目前生命徵象穩定意識清楚"
"""現造的假前綴，不是真實內容。長度要 > 24 才進得了 n-gram 判準。"""


def _guard():
    buf = _io.StringIO()
    g = jc.OutputGuard(buf)
    g.watch(("前綴", SECRET))
    return g, buf


def test_守門讓計數與群編號通過():
    g, buf = _guard()
    g.write("  [1/23] A001 ✓  已判 1（0.0190 USD，累計 12s）\n")
    assert "A001" in buf.getvalue()


def test_守門擋下整段明文():
    g, _ = _guard()
    with pytest.raises(SystemExit):
        g.write(SECRET)


def test_守門擋下跨_write_接縫的明文():
    """`print(a, b)` 會分成好幾次 write。禁字被切在兩次之間仍要擋住——
    不補接縫的話，這道守門用 print 就能繞過去。"""
    g, _ = _guard()
    with pytest.raises(SystemExit):
        g.write(SECRET[:15])
        g.write(SECRET[15:])


def test_守門的中止訊息不含被擋下的明文():
    """**印出來這道守門自己就是外洩路徑。**"""
    g, _ = _guard()
    with pytest.raises(SystemExit) as exc:
        g.write(SECRET)
    assert SECRET[:24] not in str(exc.value)
    assert "前綴" in str(exc.value)          # 只說標籤


def test_守門不誤報短的共同片段():
    """n=24 是刻意訂寬的，與 reason_leaks_prefix 同一個門檻。"""
    g, buf = _guard()
    for word in ("使用者", "這個群", "外框佔內容比例高"):
        g.write(word)
    assert buf.getvalue()


def test_守門的禁字換群就換():
    g, _ = _guard()
    g.watch(("前綴", "另一群的前綴" * 8))
    g.write(SECRET)          # 上一群的前綴不再是禁字


def test_clear_之後不再擋():
    g, buf = _guard()
    g.clear()
    g.write(SECRET)
    assert buf.getvalue() == SECRET


def test_reason_也是禁字():
    g, _ = _guard()
    g.watch(("前綴", "短"), ("reason", SECRET))
    with pytest.raises(SystemExit) as exc:
        g.write(SECRET)
    assert "reason" in str(exc.value)


def test_裝上與卸下會還原_stdout_與_stderr():
    before = (sys.stdout, sys.stderr)
    guard = jc.install_output_guard()
    try:
        assert isinstance(sys.stdout, jc.OutputGuard)
        assert isinstance(sys.stderr, jc.OutputGuard)
        # stderr 也要包：traceback 走 stderr，而 traceback 會把區域變數
        # 格式化出來，那條路徑上的區域變數包含前綴。
        guard.watch(("前綴", SECRET))
        with pytest.raises(SystemExit):
            sys.stderr.write(SECRET)
    finally:
        jc.uninstall_output_guard()
    assert (sys.stdout, sys.stderr) == before


def test_卸下兩次不會出事():
    jc.install_output_guard()
    jc.uninstall_output_guard()
    jc.uninstall_output_guard()


def test_真的跑一輪時守門是裝上的(monkeypatch, tmp_path, capsys):
    """不是只測類別——測 main 有沒有真的把它接上去。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001"],
                      [_probe(), _judgement(0.02)])
    assert jc.main([]) == 0
    assert "OutputGuard" in capsys.readouterr().out
    # 而且跑完要還原，否則後面的測試全部帶著守門跑
    assert not isinstance(sys.stdout, jc.OutputGuard)


# --- --output：重跑而不覆蓋 -------------------------------------------------
def test_output_寫到別的檔而不是預設檔(monkeypatch, tmp_path):
    """同指紋重跑一批已判過的群，量的是純雜訊。**不可以就地覆蓋**——
    覆蓋掉第一次的答案，就沒有東西可以跟第二次比。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001"],
                      [_probe(), _judgement(0.02)])
    other = tmp_path / "rerun1.csv"
    assert jc.main(["--output", str(other)]) == 0
    assert other.exists()
    assert not out.exists()


def test_output_讓已判過的群重新判(monkeypatch, tmp_path):
    """續跑機制看的是輸出檔裡的 group_id。換一個檔，已判的就該重判——
    這正是重跑要的行為。"""
    fake, out = _wire(monkeypatch, tmp_path, ["A001"],
                      [_probe(), _judgement(0.02), _probe(), _judgement(0.02)])
    assert jc.main([]) == 0
    other = tmp_path / "rerun1.csv"
    assert jc.main(["--output", str(other)]) == 0
    with other.open(encoding="utf-8-sig", newline="") as fh:
        assert [r["group_id"] for r in csv.DictReader(fh)] == ["A001"]


def test_output_不影響指紋():
    """**指紋只認判定條件。** 寫到哪個檔不是判定條件——把它算進去，
    重跑一批來量雜訊就會變成「另一個實驗」，而那正是要比對的東西。"""
    assert _real_fingerprint() == CURRENT_FINGERPRINT


CURRENT_FINGERPRINT = (
    "a0d19daab4bd80a12ade1061af1464711b85a1854edfc05ca04ad98ef01b8efb")
"""現行指紋。**改動它要跟全量重跑一起做，不是改個數字讓測試變綠。**

釘在測試裡的理由：指紋在每一列輸出上都有，但沒有東西在讀它——
`stale_fingerprints` 只在同一個檔案內比對，跨檔、跨次執行沒有人看。
一個不小心改到模板的編輯會安靜地換掉指紋，而輸出檔看起來一模一樣。
見 `ref/annotation_protocol.md`〈判定器的重跑條件〉。
"""
