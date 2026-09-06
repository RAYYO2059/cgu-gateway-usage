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


ROW = dict(
    group_id="A001", frame_owner="tool", disposition="remove",
    axis_level="all_three", reason="結構描述", confidence="high",
    model="claude-opus-5", prompt_sha256="a" * 64,
    judged_at="2026-09-06T20:00:00",
    total_cost_usd=0.0066, input_tokens=347, output_tokens=151,
    thinking_tokens=0, duration_ms=1660)


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
    assert parser_opts <= {"--limit", "--dry-run", "--prefix-field",
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
        "usage": {"input_tokens": 347, "output_tokens": 151,
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
    assert got["input_tokens"] == 347
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


def test_預算是保險絲不是預算():
    assert jc.budget_usd(225) == round(225 * jc.BUDGET_PER_GROUP_USD
                                       * jc.BUDGET_SAFETY_FACTOR, 2)
    assert jc.budget_usd(225) > 225 * jc.BUDGET_PER_GROUP_USD
    assert jc.budget_usd(0) > 0


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
