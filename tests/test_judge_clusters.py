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

# **合成值域，刻意不跟著真實值域走。** `parse_judgement` 收哪一組值是由參數
# 決定的，用真實值域測等於把「解析器對不對」與「值域是什麼」綁在一起——
# 值域改一次，這一批與解析無關的測試就要全改一次。
FIELDS = {
    "frame_owner": ("tool", "user", "none", "unsure"),
    "disposition": ("remove", "keep", "split", "unsure"),
    "axis_level": ("all_three", "invocation_only", "none", "unsure"),
}

# 真實值域。**用到它的是那些會碰到載具的測試**，所以從載具取，不抄一份。
CARRIER_FIELDS = jc._load_carrier().FIELD_HELP
FIRST = {ax: next(iter(vals)) for ax, vals in CARRIER_FIELDS.items()}


ROW = dict(
    group_id="A001", frame_owner=FIRST["frame_owner"],
    disposition=FIRST["disposition"], axis_level=FIRST["axis_level"],
    reason="結構描述", confidence="high",
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


def test_失控偵測線與逐次偵測線是兩個數字():
    assert jc.PER_CALL_BUDGET_USD != jc.RUNAWAY_ABORT_USD
    assert jc.RUNAWAY_ABORT_USD > jc.PER_CALL_BUDGET_USD


def test_逐次乘上群數會超過整批的偵測線():
    """**作用域的最大情況**，也是整批偵測存在的理由：225 次各自守住逐次
    的線，加起來仍然可以是整批那條線的兩倍以上。旗標擋不到這件事。"""
    assert 225 * jc.PER_CALL_BUDGET_USD > jc.RUNAWAY_ABORT_USD


def test_失控偵測線高於實際估計值():
    """225 群的估計 = 225 次判定 + 9 次盲化檢查。偵測線要在正常值之上，
    否則正常執行就會觸發，而一個常態誤報的檢查最後一定會被關掉。"""
    estimate = 225 * jc.MEASURED_PER_CALL_USD + 9 * jc.MEASURED_PROBE_USD
    assert jc.RUNAWAY_ABORT_USD > estimate * 2


def test_累計超過才停():
    b = jc.RunawayDetector(cap_usd=0.10)
    for _ in range(3):
        b.spend(0.03)
    assert b.spent == pytest.approx(0.09)
    assert b.exceeded() is False


def test_剛好等於偵測線不停():
    """上限是「不准超過」不是「不准達到」。反過來會少判一群而且沒有徵兆。"""
    b = jc.RunawayDetector(cap_usd=0.10)
    b.spend(0.10)
    assert b.exceeded() is False
    b.spend(0.0001)
    assert b.exceeded() is True


def test_記得停在哪一群():
    b = jc.RunawayDetector(cap_usd=0.05)
    b.spend(0.06)
    assert b.exceeded() is True
    b.stop("A042")
    assert b.stopped_at == "A042"


def test_一開始沒有停在任何地方():
    assert jc.RunawayDetector().stopped_at is None


def test_成本缺值另外計數而不是當成零():
    """缺值當 0 累加，整批上限就悄悄失效了——收尾必須看得出來。"""
    b = jc.RunawayDetector(cap_usd=1.0)
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
    # **從 META_FIELDS 推導，不手抄一份。** 手抄的那份會與 META_FIELDS 漂掉
    # ——加一個欄位，這裡就 KeyError；而在真的加欄之前，它一直是綠的，
    # 看不出它保護的範圍已經比實際的中繼資料少了一欄。
    _VALUES = {
        "uid數": 3, "相異內容數": 40, "請求數": 120,
        "前綴佔中位內容長度比例": 0.4, "unit_type分布": "{}",
        "request_style分布": "{}", "內容長度_中位": 800,
        "隱藏上下文請求佔比": 0.0,
    }
    missing = [k for k in jc.META_FIELDS if k not in _VALUES]
    assert not missing, f"META_FIELDS 新增了 {missing}，這裡要補一個假值"
    meta = pd.DataFrame([{"group_id": g,
                          **{k: _VALUES[k] for k in jc.META_FIELDS}}
                         for g in group_ids])
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
    axes = jc.render_axes(carrier.FIELDS, carrier.FIELD_HELP,
                          carrier.AXIS_CONSTRAINTS)
    return jc.template_fingerprint(axes, "raw", runtime="claude_code",
                                   flags=jc.cli_flags(schema), schema=schema)


def _judgement(cost):
    """一筆合法的判定回覆。三軸的值取值域裡的第一個，內容不重要。"""
    body = _json.dumps({**FIRST, "reason": "結構描述",
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
    """豁免要寫在**被執行的那份文字**裡，不是寫在旁邊的說明裡。

    沿革：最早那個豁免只在 docstring 裡，探針的第 (2) 項字面上仍涵蓋
    環境資訊；2026-09-07 把它寫進問句，成為一句附註（「例外：…不算」）。
    **附註不夠**——它要模型自己界定「眼前這段算不算例外」，而模型每次
    界定得不一樣。2026-09-10 改成兩份清單（【不算】列在前、【算】列在後），
    並明寫「只有【不算】清單上的東西 → NO」。

    量測（真實路徑，即 `blindness_check` 逐字相同的旗標構造）：

        附註版   n=12，YES 7 次 = 58%
                 同一批的 prompt_tokens_total 1280–1283，
                 YES（1281–1283）與 NO（1280–1282）**完全重疊**
                 ——上下文沒變，變的是模型對附註的讀法
        判定器啟動時的實際擋下率：10 次判決擋下 4 次

    這一版之前的 docstring 記著「16 次 15 NO / 1 YES」，與上面兩個數字
    都對不上；那次量測的構造沒有留下紀錄，**不採信，不並排**。
    見 `docs/ENGINEERING_NOTES.md`〈並排的數字要標明各自的算法〉。
    """
    probe = jc.BLIND_PROBE_PROMPT
    # 豁免要是一份清單，不是一句要模型自己界定的附註
    assert "【不算】" in probe
    assert probe.index("【不算】") < probe.index("【算】"), "不算的清單要在前面"
    assert "當前日期" in probe
    assert "系統提示本身" in probe
    # 豁免必須是窄的：只放行系統提示與環境資訊，且要明講身分也不算
    assert "即使它指出使用者是誰，也不算" in probe
    # 真正危險的三項一項都不能被放行
    for must in ("CLAUDE.md", "先前的對話紀錄", "檔案系統"):
        assert must in probe, must
    # 「只有不算的東西 → NO」這個對應要寫死，不能只列清單
    assert "→ answer 回 NO" in probe


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
    "e13f9738fba32a8f5167807157718758954dc2e2f1304392bdf86b3b7dd97114")
"""現行指紋。**改動它要跟全量重跑一起做，不是改個數字讓測試變綠。**

沿革（每一次都要寫明換的理由，否則下一個人分不出「有意的」與「改綠的」）：

- `79635e2b…`  107 群那一批
- `a0d19daab…` 探針問法改寬；測試台 A001–A023 判了兩次
- `240ffe175…` （commit e45b500，2026-09-08 20:05:01）disposition 值域從
  remove／keep／split 改成 tool_injected／batch_project／service_relay／
  user_envelope／insufficient，frame_owner 的 tool／user 改用散布度定義。
  **既有 130 筆判定的 disposition 值不在新值域裡，不可與新結果並排。**
- `a2dc7b114…` **本次**：axis_level 值域從 all_three／invocation_only／none
  改成 all_three／invocation_only／not_attributable／insufficient_data／none。
  拆出 `not_attributable`（意圖不屬於本 gateway 使用者，對應 service_relay）
  與 `insufficient_data`（材料被上游拿掉，對應 insufficient），
  `none` 收窄成「判不出來而且不知道為什麼」；`invocation_only` 的定義文字
  補上「role/domain 需抽樣」。**`240ffe17` 下判定了 0 筆，所以這次換指紋
  沒有作廢任何既有判定。**
- `83086cd22…` **本次**（2026-09-10）：兩處改動一起換。
  (1) 盲化探針措辭改寫——舊問法把「什麼不算」寫成一句附註要模型自己界定，
      實測偽陽性率 58%（真實路徑 n=12，YES 與 NO 的 prompt_tokens_total
      完全重疊）。新問法把「不算」列成清單放前面，並明寫「只有這些 → NO」。
  (2) 軸間約束進提示詞（載具的 `AXIS_CONSTRAINTS`，經 `render_axes` 併入
      `axes_block`）。理由：118 群實測 5 個 tool_injected 群**全部**給了
      實質 axis_level，沒有一個填 none——約束只寫在字典與協定裡，
      判定器讀不到。
  **`a2dc7b11` 的 118 筆判定因此作廢**，保留為
  `judge_output.a2dc7b11.constraint_absent.csv`，用途是量「加約束前後
  axis_level 變了多少、disposition 動了沒有」。
- `e13f9738f…` **本次**（2026-09-10）：`META_FIELDS` 補
  `隱藏上下文請求佔比`（群內符合 `prompt_tokens > 4 × prompt_text_len`
  的請求佔比）。理由：`insufficient` 的判準就是這個門檻，而判定器一直
  收不到 `prompt_tokens`——**判準寫了但不可執行**，兩次全量執行
  `insufficient` 都是 0 群。
  `judge_output.83086cd2.csv` 因此作廢，保留為比對用。

釘在測試裡的理由：指紋在每一列輸出上都有，但沒有東西在讀它——
`stale_fingerprints` 只在同一個檔案內比對，跨檔、跨次執行沒有人看。
一個不小心改到模板的編輯會安靜地換掉指紋，而輸出檔看起來一模一樣。
見 `ref/annotation_protocol.md`〈判定器的重跑條件〉。
"""


# --- 盲化的主要指標：prompt_tokens_total ------------------------------------
#
# 成本降級成次要訊號的理由是量出來的：同指紋、同 23 群、同提示詞跑兩次，
# 成本差 1.53 倍（快取狀態不同），而盲化失效的訊號是 2.4 倍——兩者同一
# 量級，成本區分不開它們。prompt_tokens_total 對快取不變（兩次中位
# 1,872 對 1,883）。

def test_單輪帶的上下界來自實測():
    assert jc.PROMPT_TOKENS_SINGLE_MIN < jc.PROMPT_TOKENS_SINGLE_MAX
    assert jc.PROMPT_TOKENS_WARN > jc.PROMPT_TOKENS_SINGLE_MAX


def test_實測的單輪呼叫不會被判為可疑():
    """138 次單輪呼叫落在 1,785–2,515。門檻壓進這個區間就會常態誤報。"""
    for n in (jc.PROMPT_TOKENS_SINGLE_MIN, 2000, jc.PROMPT_TOKENS_SINGLE_MAX):
        assert jc.prompt_tokens_suspicious(n) is False


def test_實測的兩輪呼叫會被判為可疑():
    """實測兩輪落在 3,847–5,141。它不是盲化問題，但它確實超出單輪帶，
    該被記下來——**單次分不開兩輪與未盲化**，那是最小值那道的工作。"""
    for n in (3847, 5141):
        assert jc.prompt_tokens_suspicious(n) is True


def test_token_缺值不算可疑():
    """舊格式的列沒有這一欄。「沒量到」不是「不正常」。"""
    assert jc.prompt_tokens_suspicious(None) is False
    assert jc.prompt_tokens_suspicious("") is False


def test_最小值檢查抓得到全批抬高():
    """預設系統提示是加在每一次呼叫上的常數，抬高的是最小值。"""
    assert jc.prompt_tokens_floor_broken(jc.PROMPT_TOKENS_SINGLE_MIN) is False
    assert jc.prompt_tokens_floor_broken(jc.PROMPT_TOKENS_SINGLE_MAX) is False
    assert jc.prompt_tokens_floor_broken(6000) is True


def test_兩輪呼叫抬不動最小值():
    """**這是最小值那道比單次那道強的地方。** 一批裡混著兩輪呼叫時，
    最小值仍然落在單輪帶——所以它不會把「跑了兩輪」誤判成「盲化失效」。"""
    batch = [1785, 1900, 5141, 2515, 4203]      # 混著兩輪的實測值
    assert jc.prompt_tokens_floor_broken(min(batch)) is False
    assert sum(jc.prompt_tokens_suspicious(n) for n in batch) == 2


def test_一次都沒量到時不下結論():
    """**空集合不算通過也不算失敗。** 斷言在集合為空時自動成立，
    是本專案用突變測試抓過的「空真」。"""
    assert jc.prompt_tokens_floor_broken(None) is False


def test_收尾會報出實際量到的_token_最小值(monkeypatch, tmp_path, capsys):
    """**斷言值，不是斷言字串出現。**

    第一版寫的是 `assert "prompt_tokens_total" in out`，而「沒有量到」
    那個分支也印同一個詞——於是「最小值從不更新」這個突變體逃掉了，
    測試照樣全綠。這正是本專案的「空真」：斷言成立，但什麼都沒驗。
    """
    fake, out = _wire(monkeypatch, tmp_path, ["A001"],
                      [_probe(), _judgement(0.02)])
    assert jc.main([]) == 0
    text = capsys.readouterr().out
    # _envelope 的 usage：input 2 + cache_creation 812 + cache_read 0
    assert "最小 814" in text
    assert "沒有量到" not in text


# --- 用量上限與呼叫失敗要分得開 ---------------------------------------------
def test_用量上限與一般失敗分成不同的類():
    assert jc.classify_call_error(1, "Claude usage limit reached") == "usage_limit"
    assert jc.classify_call_error(1, "429 rate limit exceeded") == "transient"
    assert jc.classify_call_error(1, "some other explosion") == "failure"


def test_分不出來時當成一般失敗():
    """**保守的方向是 failure**：它會被記錄、被跳過、下次續跑會再試。
    誤判成 usage_limit 的代價是整批提早停而看起來像跑完了。"""
    for junk in (None, "", "???", "unhelpful message"):
        assert jc.classify_call_error(1, junk) == "failure"


def test_同時出現兩種字樣時判為用量上限():
    """兩邊都可能出現 limit。誤判成 transient 的代價是退避五次然後把整批
    記成失敗；反過來只是早停，可以直接續跑。"""
    assert jc.classify_call_error(
        1, "rate limit; usage limit reached") == "usage_limit"


def test_分類不會回傳_stderr_的內容():
    """stderr 可能夾帶提示詞——它是我們自己從 stdin 餵進去的東西。"""
    secret = "病患主訴胸悶合併呼吸困難已持續三日目前生命徵象穩定"
    got = jc.classify_call_error(1, f"error: {secret}")
    assert got in ("usage_limit", "transient", "failure")
    assert secret not in got


def test_用量上限不走退避重試(monkeypatch):
    """退避管的是秒級的暫時問題；用量上限等的是小時級的重置。
    在這裡重試五次只是把 190 秒浪費掉，然後仍然失敗。"""
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise jc.UsageLimitReached("訂閱用量上限")

    monkeypatch.setattr(jc, "run_claude", boom)
    monkeypatch.setattr(jc.time, "sleep", lambda s: None)
    with pytest.raises(jc.UsageLimitReached):
        jc.call_with_backoff("p", [], Path("."))
    assert len(calls) == 1


def test_一般失敗仍然會退避重試(monkeypatch):
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise ValueError("一般失敗")

    monkeypatch.setattr(jc, "run_claude", boom)
    monkeypatch.setattr(jc.time, "sleep", lambda s: None)
    with pytest.raises(ValueError):
        jc.call_with_backoff("p", [], Path("."))
    assert len(calls) == jc.MAX_RETRIES + 1


def test_撞到用量上限就停而不是把剩下的群都記成失敗(monkeypatch, tmp_path, capsys):
    """**這是分類存在的理由。** 接下來每一群都會撞到同一件事，把它們全部
    記成失敗是錯的——它們只是還沒判，而「失敗 200 次」與「沒跑 200 群」
    在收尾訊息裡是兩個完全不同的意思。"""
    ids = ["A001", "A002", "A003"]
    fake, out = _wire(monkeypatch, tmp_path, ids, [_probe(), _judgement(0.02)])
    real = jc.call_with_backoff

    def limited(prompt, flags, cwd):
        if len(_read(out)) >= 1:
            raise jc.UsageLimitReached("訂閱用量上限")
        return real(prompt, flags, cwd)

    monkeypatch.setattr(jc, "call_with_backoff", limited)
    assert jc.main([]) == 0
    text = capsys.readouterr().out
    assert "用量上限" in text
    assert "失敗：呼叫 0" in text          # 沒有被記成失敗
    assert len(_read(out)) == 1           # 判成功的那一群留著


def _read(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# --- 協定裡的數字要被機械檢查，不要靠人記得更新 ------------------------------
#
# `ref/annotation_protocol.md` 第三節記了測試台的基準一致率。那是手工維護的
# 數字，而本專案的手工數字每一輪都會過期（CLAUDE.md 的 444 → 456 → 470，
# 兩輪內錯兩次）。這裡把它接回實際的輸出檔。
#
# 判定結果在 repo 外，找不到就 skip 而不是 fail——與判定器本身同一個處理。

PROTOCOL = REPO / "ref" / "annotation_protocol.md"
OUTPUTS = {
    "79635e2b": JUDGE.parent / "judge_output.79635e2b.csv",
    "a0d19daa#1": JUDGE.parent / "judge_output.csv",
    "a0d19daa#2": JUDGE.parent / "judge_output.a0d19daa.rerun1.csv",
}


def _judgements():
    """三批共有的群 × 三次判定。缺任何一批就 skip。"""
    missing = [k for k, v in OUTPUTS.items() if not v.exists()]
    if missing:
        pytest.skip(f"判定輸出不在本機（缺 {', '.join(missing)}）")
    frames = {k: pd.read_csv(v).set_index("group_id") for k, v in OUTPUTS.items()}
    ids = sorted(set.intersection(*[set(f.index) for f in frames.values()]))
    return frames, ids


def _agreement(frames, ids, axis):
    """三次全同的比例。"""
    same = sum(len({frames[k].loc[g, axis] for k in frames}) == 1 for g in ids)
    return 100.0 * same / len(ids)


def _pairwise(frames, ids, axis):
    """同指紋兩次的一致率。**判讀規則用的是這個，不是三次全同。**"""
    a, b = frames["a0d19daa#1"], frames["a0d19daa#2"]
    same = sum(a.loc[g, axis] == b.loc[g, axis] for g in ids)
    return 100.0 * same / len(ids)


@pytest.mark.parametrize("axis,three_way,pairwise", [
    ("frame_owner", 91.3, 91.3),
    ("disposition", 69.6, 82.6),
    ("axis_level", 78.3, 87.0),
    ("confidence", 69.6, 91.3)])
def test_協定記的基準一致率與實際輸出相符(axis, three_way, pairwise):
    """對不上就是協定過期了——**改協定，不要改這個測試的期望值**。

    **兩欄都要驗。** 第一版只驗了一欄，而協定裡四個數字有兩個填成另一種
    算法的值（`axis_level` 與 `confidence` 填了成對值、`disposition` 填了
    三次值），並排看不出來。兩欄一起釘住，混用就會失敗。
    """
    frames, ids = _judgements()
    assert _agreement(frames, ids, axis) == pytest.approx(three_way, abs=0.05)
    assert _pairwise(frames, ids, axis) == pytest.approx(pairwise, abs=0.05)


def test_兩種算法在協定裡都出現而且分得開():
    """`frame_owner` 兩欄剛好相同是巧合，其他三軸都不同——所以協定若只記
    一欄，另一欄的使用者就會拿錯數字，而拿錯不會有任何徵兆。"""
    frames, ids = _judgements()
    diff = [ax for ax in ("disposition", "axis_level", "confidence")
            if _agreement(frames, ids, ax) != _pairwise(frames, ids, ax)]
    assert diff == ["disposition", "axis_level", "confidence"]
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "三次全同" in text and "同指紋兩次" in text


def test_協定記的解析度與測試台大小相符():
    """一群 = 100/n。n 變了解析度就變，而協定裡兩個數字都是寫死的。"""
    frames, ids = _judgements()
    text = PROTOCOL.read_text(encoding="utf-8")
    assert f"n={len(ids)}" in text
    assert f"{100 / len(ids):.1f}pp" in text          # 4.3pp
    assert f"{200 / len(ids):.1f}pp" in text          # 8.7pp


def test_協定列的受污染群就是實際不穩定的那些():
    """七群是「因模型不穩定而被選出」——這個描述本身要成立。

    若之後重跑讓某一群穩定下來，名單不會自己更新，而一份過期的污染名單
    比沒有名單更危險：它會讓人以為某些群是乾淨的。
    """
    frames, ids = _judgements()
    unstable = {g for g in ids
                if len({frames[k].loc[g, "disposition"] for k in frames}) > 1}
    text = PROTOCOL.read_text(encoding="utf-8")
    listed = {g for g in ids if f"{g}／" in text or f"／{g}" in text}
    assert listed == unstable, f"協定列的 {sorted(listed)} vs 實際 {sorted(unstable)}"


def test_三軸定義改了指紋就會變():
    """**這是第三節第 2 點的機械證明。**

    字典的邊界要讓判定器看到，就得改 `FIELD_HELP`；而 `FIELD_HELP` 進指紋，
    所以「字典改後」那兩次必然是新指紋，與基準線的比較必然跨指紋。
    這不是可以避免的安排，是這個測量的定義本身。
    """
    carrier = jc._load_carrier()
    before = jc.template_fingerprint(
        jc.render_axes(carrier.FIELDS, carrier.FIELD_HELP))
    tweaked = {axis: dict(vals) for axis, vals in carrier.FIELD_HELP.items()}
    tweaked["disposition"][FIRST["disposition"]] += "（補一條邊界定義）"
    after = jc.template_fingerprint(jc.render_axes(carrier.FIELDS, tweaked))
    assert before != after


def test_判定器不讀_label_dictionary():
    """第三節第 1 點：只改 `label_dictionary.csv` 判定器看不到。

    釘住它是因為這件事**看起來應該不成立**——檔案叫「標籤字典」，
    直覺會以為判定器讀它。實際上三軸定義取自載具的 `FIELD_HELP`。
    哪天有人把它接起來了，這個測試會失敗，而那正是該重新看第三節的時候。
    """
    source = JUDGE.read_text(encoding="utf-8")
    assert "label_dictionary" not in source
    assert "FIELD_HELP" in jc.CARRIER.read_text(encoding="utf-8")


# --- 現行方案（跑未判定的群，比分布）的前提也要被機械檢查 --------------------
#
# 協定第三節換了方案之後，多了一批手工維護的數字：三個集合的群數與涵蓋量、
# `user`→`remove` 的基準。它們全部來自輸出檔與 `clusters.parquet`，
# 所以全部接得回去。接不回去就是協定過期了——**改協定，不要改期望值**。

SCREEN = JUDGE.parent / "prefix_screen.csv"
CLUSTERS = JUDGE.parent / "clusters.parquet"


def _group_sets():
    """clean 群的三層切分：測試台 / 僅舊指紋判過 / 完全未判定。"""
    need = [SCREEN, CLUSTERS] + list(OUTPUTS.values())
    missing = [p.name for p in need if not p.exists()]
    if missing:
        pytest.skip(f"資料不在本機（缺 {', '.join(missing)}）")
    screen = pd.read_csv(SCREEN)
    clean = set(screen.loc[screen["personal_data"] == "clean", "group_id"])
    old = set(pd.read_csv(OUTPUTS["79635e2b"])["group_id"])
    bed = set(pd.read_csv(OUTPUTS["a0d19daa#1"])["group_id"])
    return {
        "測試台": sorted(bed),
        "僅舊指紋": sorted(old - bed),
        "完全未判定": sorted(clean - old - bed),
        "clean": sorted(clean),
    }


@pytest.mark.parametrize("name,groups,contents,requests", [
    ("測試台", 23, 4422, 20249),
    ("僅舊指紋", 84, 1776, 4304),
    ("完全未判定", 118, 5444, 7826),
    ("clean", 225, 11642, 32379)])
def test_協定記的群集切分與涵蓋量與實際檔案相符(name, groups, contents, requests):
    """三個集合的群數、相異內容數、請求數。

    **三個都要驗，不能只驗群數。** 協定用它們論證「只報群數會把一群兩萬筆
    請求的結構當成一群五筆的同等份量」——那個論證靠的正是涵蓋量。
    """
    sets = _group_sets()
    ids = sets[name]
    assert len(ids) == groups
    c = pd.read_parquet(CLUSTERS).set_index("group_id").loc[ids]
    assert int(c["相異內容數"].sum()) == contents
    assert int(c["請求數"].sum()) == requests
    # 三個數字要出現在**同一列**上。分開找會近乎空真——「23」在這份檔裡
    # 到處都是（`n=23`、`A001–A023`），單獨比對它等於沒比對。
    text = PROTOCOL.read_text(encoding="utf-8")
    wanted = [f"{n:,}" for n in (groups, contents, requests)]
    hit = [ln for ln in text.splitlines()
           if ln.lstrip().startswith("|") and all(w in ln for w in wanted)]
    assert hit, f"協定裡沒有一列同時有 {wanted}"


def test_協定沒有把_202_當成完全未判定():
    """一度寫錯的那件事：「剩下 202 群」是以現行指紋為準的未判定數，
    其中 84 群在舊指紋下已經判過。**「未判定」這個詞不帶指紋**，
    而分母的定義隨指紋而變——所以協定必須明寫這兩層。
    """
    sets = _group_sets()
    assert len(sets["僅舊指紋"]) + len(sets["完全未判定"]) == 202
    assert len(sets["完全未判定"]) != 202          # 這就是錯誤的內容
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "「202 群完全未判定」不成立" in text
    assert "主結果用 118 群" in text


@pytest.mark.parametrize("scope,user_n,remove_n", [
    ("測試台69", 42, 4),
    ("僅舊指紋84", 53, 0),
    ("全部130", 80, 3)])
def test_協定記的_user_remove_基準與實際輸出相符(scope, user_n, remove_n):
    """預期方向是拿這三個數字寫定的。

    **關鍵是 `僅舊指紋84` 那一列的 0。** 它讓「收斂到 0」變成不可證的方向，
    也讓判準必須改寫成「超過 5.7%」。若哪天它不再是 0，預期方向就得重寫，
    而重寫的時機是這個測試失敗的時候，不是有人剛好想起來的時候。
    """
    frames, ids = _judgements()
    sets = _group_sets()
    old = pd.read_csv(OUTPUTS["79635e2b"])
    cur = pd.read_csv(OUTPUTS["a0d19daa#1"])
    if scope == "測試台69":
        rows = pd.concat([frames[k].loc[ids].reset_index() for k in frames])
    elif scope == "僅舊指紋84":
        rows = old[old["group_id"].isin(sets["僅舊指紋"])]
    else:
        rows = pd.concat([old, cur])
    user = rows[rows["frame_owner"] == "user"]
    assert len(user) == user_n
    assert int((user["disposition"] == "remove").sum()) == remove_n


def test_三法則的判準與基準樣本數相符():
    """0/53 的 95% 上界是 3/53 = 5.7%。n 變了門檻就變，而協定裡是寫死的。"""
    sets = _group_sets()
    old = pd.read_csv(OUTPUTS["79635e2b"])
    n = int((old[old["group_id"].isin(sets["僅舊指紋"])]["frame_owner"] == "user").sum())
    # 去掉 markdown 的粗體標記再比——強調記號會插在數字中間，
    # 而「協定裡有沒有這個數字」跟它有沒有被加粗無關。
    text = PROTOCOL.read_text(encoding="utf-8").replace("**", "")
    assert f"0/{n}" in text
    assert f"3/{n} = {300 / n:.1f}%" in text


def test_協定對_frame_owner_兩欄相同的解釋成立():
    """兩欄相同的機制是「三次不穩的那些群，在成對比較的那兩批之間就已經
    不同」——**不是「異值都落在同一批」**。後者是本檔一度寫過的說法，
    而它是錯的：A017 的異值在 `a0d19daa#1`，A019 的在 `#2`。

    這個測試釘的是解釋，不是數字。解釋錯了數字照樣對得上，所以它不會被
    `test_協定記的基準一致率與實際輸出相符` 抓到。
    """
    from collections import Counter
    frames, ids = _judgements()
    three = {g for g in ids
             if len({frames[k].loc[g, "frame_owner"] for k in frames}) > 1}
    pair = {g for g in ids
            if frames["a0d19daa#1"].loc[g, "frame_owner"]
            != frames["a0d19daa#2"].loc[g, "frame_owner"]}
    assert three == pair, "兩欄相同的機制不成立了，協定的解釋要重寫"
    odd = {}
    for g in three:
        vals = {k: frames[k].loc[g, "frame_owner"] for k in frames}
        seen = Counter(vals.values())
        odd[g] = next(k for k, v in vals.items() if seen[v] == 1)
    assert len(set(odd.values())) > 1, f"異值批次 {odd}"
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "不是「異值都落在同一批」" in text


def test_作廢的方案連同理由留在協定裡():
    """預先寫定的規則若可以被安靜換掉，它就不再是預先寫定的。

    2026-09-07 的 23 群測試台方案已作廢，但四條作廢理由與原方案都要留著
    ——留著才看得出換過，也才看得出換的理由不是事後配合結果編的。
    """
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "作廢的方案：23 群測試台重跑量一致率" in text
    assert "2026-09-07 初版" in text and "2026-09-08 改版" in text
    for marker in ("**(a) 判定器看不到字典。**",
                   "**(b) 要讓判定器看到就得改 `FIELD_HELP`",
                   "**(c) 跨指紋的雜訊剛好等於判讀門檻。**",
                   "**(d) 那 23 群本來就不可用於驗證。**"):
        assert marker in text, marker


# --- 驗證涵蓋率：三組分母，同一列上要全部對得起來 --------------------------
#
# 協定第三節〈驗證涵蓋率的限制〉整段論證靠的是「請求量集中在已污染的 23 群」。
# 那是五個數字撐起來的一列，改壞其中一個不會有任何徵兆——所以整列一起釘。

def _section(heading: str) -> str:
    """協定裡某一節的內文（到下一個同級或更高級標題為止），去掉粗體標記。

    列名在不同的表裡會重複，所以比對必須限節——不限節的比對會抓到別張表
    的同名列，而那張表的數字是另一件事。
    """
    text = PROTOCOL.read_text(encoding="utf-8").replace("**", "")
    lines = text.splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith(heading))
    level = len(heading) - len(heading.lstrip("#"))
    out = []
    for line in lines[i + 1:]:
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            break
        out.append(line)
    return "\n".join(out)


FROZEN_CONTENTS = 17365          # 分類母數，第一節〈基準線〉凍結
FROZEN_REQUESTS = 58100


def test_協定第一節的分類母數還是凍結的那組():
    """下面的涵蓋率用它當分母。它變了，涵蓋率整張表都要重算。"""
    text = PROTOCOL.read_text(encoding="utf-8")
    assert f"（{FROZEN_CONTENTS:,} 個相異內容 / {FROZEN_REQUESTS:,} 筆請求）" in text


@pytest.mark.parametrize("name,label", [
    ("測試台", "測試台 23（已污染）"),
    ("僅舊指紋", "僅舊指紋 84"),
    ("完全未判定", "完全未判定 118"),
    ("clean", "clean 225 合計")])
def test_協定記的驗證涵蓋率與實際檔案相符(name, label):
    """相異內容、佔母數、請求、佔母數、佔 clean——**五格同列一起驗**。

    分開驗每一格會近乎空真：百分比只有三個字元，`24.2%` 這種字串在別處
    也可能出現。而這一列的意義正在於五格互相對得起來——
    「7,826 筆佔 clean 的 24.2%、佔分類母數只有 13.5%」是同一件事的兩種
    講法，其中一個改了另一個沒改，論證就變成假的而表格看起來還是整齊的。
    """
    sets = _group_sets()
    c = pd.read_parquet(CLUSTERS).set_index("group_id")
    ids = sets[name]
    contents = int(c.loc[ids, "相異內容數"].sum())
    requests = int(c.loc[ids, "請求數"].sum())
    clean_requests = int(c.loc[sets["clean"], "請求數"].sum())
    cells = [
        f"{contents:,}",
        f"{100 * contents / FROZEN_CONTENTS:.1f}%",
        f"{requests:,}",
        f"{100 * requests / FROZEN_REQUESTS:.1f}%",
        "100%" if name == "clean" else f"{100 * requests / clean_requests:.1f}%",
    ]
    # **只在〈驗證涵蓋率的限制〉那一節裡找。** 「完全未判定 118」這個列名
    # 在代價表裡也有一列，兩張表講的是不同的東西——第一版沒有限節，
    # 測試當場撞上這件事，那正是〈並排的數字要標明各自的算法〉的形狀。
    row = [ln for ln in _section("#### 驗證涵蓋率的限制").splitlines()
           if ln.lstrip().startswith("| " + label + " |")]
    assert len(row) == 1, f"找不到（或不只一列）「{label}」"
    for cell in cells:
        assert f"| {cell} " in row[0] or f"| {cell} |" in row[0], \
            f"{label} 這一列少了 {cell}：{row[0]}"


def test_涵蓋率的三個集合相加等於_clean():
    """表格自己要加得起來。CLAUDE.md 記著一次「清單加起來是 24 而數字寫
    23，兩個都對不上卻因為沒有人相加而存活下來」——這裡把相加自動化。
    """
    sets = _group_sets()
    c = pd.read_parquet(CLUSTERS).set_index("group_id")
    parts = ["測試台", "僅舊指紋", "完全未判定"]
    for col in ("相異內容數", "請求數"):
        assert sum(int(c.loc[sets[k], col].sum()) for k in parts) \
            == int(c.loc[sets["clean"], col].sum())


def test_協定明寫大群在污染側():
    """限制的成因是「大群幾乎全在污染側」，不是「n 不夠」。

    這個判斷會隨資料改變，所以要接回檔案：clean 前三大群若哪天不再全部
    落在測試台裡，那段論證就得重寫。
    """
    sets = _group_sets()
    c = pd.read_parquet(CLUSTERS).set_index("group_id")
    top3 = list(c.loc[sets["clean"]].nlargest(3, "請求數").index)
    assert set(top3) <= set(sets["測試台"]), f"前三大群 {top3} 不再全在測試台"
    biggest_clean = c.loc[sets["完全未判定"], "請求數"].idxmax()
    # **群編號與筆數要同句。** 只找編號會抓到第一節的群集清單
    # （`A001`–`A123` 那一行），那跟「它是不是最大的群」無關——
    # 第一版就是這樣讓一個突變逃掉的。
    import re
    body = _section("#### 驗證涵蓋率的限制")
    for g in top3 + [biggest_clean]:
        n = int(c.loc[g, "請求數"])
        # 提到這個群、而且句子裡有千分位數字的每一行，都要帶對的筆數。
        # **不能只要求「有一行對」**：同一個群在這一節裡出現兩次，
        # 只驗一行的話改壞其中一處不會被發現（第一版就漏掉了這個突變）。
        # **只看散文行，表格行不算。** 〈最大的群〉那張表同一列會並排兩種
        # 單位（相異內容數與請求數），拿請求數去要求它每一格都對是錯的要求
        # ——那張表由 test_協定記的最大群與實際相符 逐格驗。
        lines = [ln for ln in body.splitlines()
                 if f"`{g}`" in ln and re.search(r"\d,\d{3}", ln)
                 and not ln.lstrip().startswith("|")]
        assert lines, f"協定的涵蓋率一節裡沒有「{g} 帶著筆數」"
        for ln in lines:
            assert f"{n:,}" in ln, f"{g} 應為 {n:,}：{ln.strip()}"


def test_公開版與完整案例互相標註():
    """假訊號那條拆成兩份：公開版沒有案例，案例在不公開的那份。

    **拆開之後最容易斷的是指路。** 兩邊各自都讀得通，所以少了指路不會有
    任何徵兆——只會讓公開版看起來像一條沒有根據的斷言。
    """
    notes = REPO / "docs" / "ENGINEERING_NOTES.md"
    text = notes.read_text(encoding="utf-8")
    assert "## 重複次數可能編碼的是位置，不是性質" in text
    assert "CLASSIFICATION_NOTES.md" in text
    assert "本條沒有附案例" in text
    private = REPO / "docs" / "CLASSIFICATION_NOTES.md"
    if not private.exists():
        pytest.skip("完整案例不在本機（不進版控）")
    assert "〈重複次數可能編碼的是位置，不是性質〉" in \
        private.read_text(encoding="utf-8")


def test_工程筆記的條目都在某個分節底下():
    """條目移動之後最容易出的錯是掉在分節之外（或黏在錯的分節尾巴）。

    本檔沒有條號，所有交叉引用都用標題——所以移動不會斷引用，
    但會讓條目落在語意不對的分節底下，而那不會有任何徵兆。
    """
    notes = REPO / "docs" / "ENGINEERING_NOTES.md"
    group, placement = None, {}
    for line in notes.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            group = line[2:].strip()
        elif line.startswith("## "):
            placement[line[3:].strip()] = group
    assert placement["差小於量測解析度時，不要從方向讀出結論"] == "計數與聚合"
    assert placement["重複次數可能編碼的是位置，不是性質"] == "計數與聚合"
    assert placement["「取前 N 個」不是抽樣，除非你知道排序規則"] == "計數與聚合"
    assert placement["兩個消費者共用一個可變物件時，重新綁定只會更新其中一個"] \
        == "沉默的失效"
    assert placement["明文邊界要涵蓋例外訊息"] == "辨識與去識別"
    assert placement["並排的數字要標明各自的算法"] == "紀錄與可重現"
    assert placement["相對指涉在寫的當下明確，在讀的當下不明確"] == "紀錄與可重現"
    assert "工程筆記" not in placement.values()          # 沒有條目落在檔頭底下


def test_工程筆記的交叉引用都指得到():
    """引用用的是標題，所以改標題就會斷——而斷了不會有任何徵兆。"""
    import re
    notes = REPO / "docs" / "ENGINEERING_NOTES.md"
    text = notes.read_text(encoding="utf-8")
    titles = {ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")}
    # 〈…〉裡的引用；跨行的用去掉換行與縮排之後再比
    flat = re.sub(r"\n\s*", "", text)
    refs = (re.findall(r"見〈(.+?)〉", flat)
            + re.findall(r"同族\**：\s*〈(.+?)〉", flat))
    assert refs, "一個引用都抓不到——正規式壞了，這條會變成空真"
    for ref in refs:
        assert ref in titles, f"引用〈{ref}〉指不到任何條目"


def _note_body(title: str) -> str:
    """某一條的本文，去掉換行與縮排（引用會跨行）。"""
    import re as _re
    text = (REPO / "docs" / "ENGINEERING_NOTES.md").read_text(encoding="utf-8")
    hit = [b for b in text.split("\n## ") if b.startswith(title)]
    assert len(hit) == 1, f"找不到（或找到多條）〈{title}〉"
    return _re.sub(r"\n\s*", "", hit[0].split("\n# ")[0])


def test_同族的兩條互相指得到():
    """**互指最容易斷的是回指那一邊。** 新條目寫「同族：舊條目」是自然的
    動作，舊條目補一句指回新條目不是——而少了它，從舊條目那邊讀不出這是
    一組，只會讀成一條孤立的規則。兩邊各驗一次。
    """
    甲 = "並排的數字要標明各自的算法"
    乙 = "相對指涉在寫的當下明確，在讀的當下不明確"
    assert f"〈{乙}〉" in _note_body(甲), "舊條目沒有指回新條目"
    assert f"〈{甲}〉" in _note_body(乙), "新條目沒有指到舊條目"


def test_相對指涉那條寫出了絕對座標要帶什麼():
    """規則要可執行。「不要用相對指涉」是禁止，不是做法——少了「改用什麼」
    那一半，讀的人只能自己發明一個，而發明出來的多半又是相對的。
    """
    entry = _note_body("相對指涉在寫的當下明確，在讀的當下不明確")
    for token in ("commit hash", "時間戳", "指紋值"):
        assert token in entry, token
    # 被禁止的講法要逐個列出來，否則只會被讀成「少用一點」
    for token in ("上一輪", "剛才", "前一次"):
        assert token in entry, token
    # 成因（執行單位 ≠ 指令單位）不可省，那是這條唯一難想到的部分
    assert "一次指令可能產生多個 commit" in entry
    # 「沒有變更」的回報也適用——這是最容易漏的一半
    assert "維持原狀" in entry


# --- 「大群在污染側」的成因：編號順序，不是不穩定 ---------------------------
#
# 協定第三節寫著成因是編號按大小遞減指派，而不是「大群比較容易不穩定」。
# 兩句話都像成立，只有一句是量出來的——所以兩句都接回檔案。

def _series(prefix: str):
    c = pd.read_parquet(CLUSTERS)
    d = c[c["group_id"].str.startswith(prefix)].copy()
    d["序數"] = d["group_id"].str[1:].astype(int)
    return d.sort_values("序數")


def test_群編號按相異內容數遞減指派():
    """這是「先跑 A001–A023 ＝ 先跑最大的 23 群」的全部根據。

    哪天重新聚類換了編號規則，這個測試會失敗，而那時第三節的成因段落與
    ENGINEERING_NOTES〈「取前 N 個」不是抽樣〉的案例都要重寫。
    """
    if not CLUSTERS.exists():
        pytest.skip("clusters.parquet 不在本機")
    a = _series("A")
    assert list(a["相異內容數"]) == sorted(a["相異內容數"], reverse=True)
    text = PROTOCOL.read_text(encoding="utf-8")
    for prefix, floor in (("A", 0.99), ("B", 0.99)):
        rho = _series(prefix)["序數"].corr(
            _series(prefix)["相異內容數"], method="spearman")
        assert rho < -floor
        assert f"{rho:.3f}".lstrip("-") in text.replace("−", "-"), \
            f"{prefix} 系列的 ρ={rho:.3f} 沒有出現在協定裡"


def test_大小與判定穩定性分不出關係():
    """協定寫的是「分不出來」，不是「反向」。

    **這個測試要釘的正是那個克制。** 中位數 80 對 140 看起來很像結論，
    而 p=0.160；若哪天 p 掉到顯著，協定那段就得改寫成一個真的結論——
    改寫的時機是這裡失敗的時候。
    """
    frames, ids = _judgements()
    if not CLUSTERS.exists():
        pytest.skip("clusters.parquet 不在本機")
    from scipy import stats
    unstable = {g for g in ids
                if len({frames[k].loc[g, "disposition"] for k in frames}) > 1}
    c = pd.read_parquet(CLUSTERS).set_index("group_id").loc[ids]
    u = c.loc[sorted(unstable), "相異內容數"]
    s = c.loc[sorted(set(ids) - unstable), "相異內容數"]
    assert u.median() < s.median()                  # 方向確實是反的
    p = stats.mannwhitneyu(u, s, alternative="two-sided").pvalue
    assert p > 0.05, "已經顯著了，協定第三節的「分不出來」要改寫"
    text = PROTOCOL.read_text(encoding="utf-8")
    assert f"中位 {u.median():.0f}" in text and f"中位 {s.median():.0f}" in text
    assert f"p={p:.3f}" in text


@pytest.mark.parametrize("col,side,label", [
    ("相異內容數", "測試台", "測試台 23"),
    ("相異內容數", "完全未判定", "完全未判定 118"),
    ("請求數", "測試台", "測試台 23"),
    ("請求數", "完全未判定", "完全未判定 118")])
def test_協定記的最大群與實際相符(col, side, label):
    """「大群」在兩種單位下答案不同——相異內容最大的群在乾淨側，
    請求最大的群在污染側。**這個對比整段論證都靠它**，所以四格全釘。
    """
    sets = _group_sets()
    c = pd.read_parquet(CLUSTERS).set_index("group_id")
    sub = c.loc[sets[side], col]
    gid, n = sub.idxmax(), int(sub.max())
    body = _section("#### 驗證涵蓋率的限制")
    rows = [ln for ln in body.splitlines() if ln.lstrip().startswith("| " + col + " |")]
    assert len(rows) == 1, f"找不到「{col}」那一列"
    assert f"`{gid}` {n:,}" in rows[0], f"{col}／{side} 應為 {gid} {n:,}：{rows[0]}"


def test_協定第二節的污染範圍是整個測試台():
    """一度寫成「驗證要用另外 16 群」——那 16 群同樣是導出材料。

    **錯的方向是放寬**，而放寬的錯誤不會有徵兆：拿它們驗證會得到一個
    比較好看的一致率，看起來像字典有效。
    """
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "適用於**全部 23 群**" in text
    assert "驗證要用另外 16 群" not in text.replace("「驗證要用另外 16 群與尚未判定的 202 群」", "")
    assert "完全未判定的 118 群是首選" in text


# --- 可分類性上限：協定第一節記的數字要接回實際資料 ------------------------
#
# 這一項是「分類的對象是什麼」的界線，不是精度問題。它的數字全部手工填進
# 協定，而其中一半來自 clean、一半來自 lite——**兩批不可互換的分母並排在
# 同一節裡**，正是〈並排的數字要標明各自的算法〉的形狀。所以逐項釘住。

HITS = REPO / "runs" / "2026-09-05T1700_prefilter" / "classify_lite" / "prefilter_hits.parquet"


def _gap_section():
    return _section("### 可分類母數 17,365 的可分類性上限")


def test_lite_的_request_只有那八個鍵():
    """缺的東西在上游就不在檔案裡，不是萃取端丟的。

    這個測試釘的是 `extract_lite` 沒有做 role 挑選——哪天有人「補上」
    system 的串接，協定那一段就要重寫，而重寫的時機是這裡失敗的時候。
    """
    src = (REPO / "src" / "extract_lite.py").read_text(encoding="utf-8")
    assert 'prompt_text = _get(record, "request.prompt_text")' in src
    for token in ("messages", "instructions", '"system"'):
        assert token not in src, f"萃取端出現了 {token}，協定第一節要重寫"
    text = _gap_section()
    for key in ("conversation_id", "endpoint", "model_requested", "prompt_length",
                "prompt_text", "provider", "stream", "thread_id"):
        assert f"`{key}`" in text


@pytest.mark.parametrize("K,requests,contents", [
    (1, 42595, 7426),
    (4, 41789, 6949),
    (8, 40461, 6840)])
def test_協定記的_token_下界與實際相符(K, requests, contents):
    """**三個 K 都要驗。** 論證靠的是「K 從 1 到 8 幾乎不動」——

    只釘 K=4 那一列的話，另外兩列可以被改成任意值而論證看起來還在。
    """
    if not HITS.exists():
        pytest.skip("prefilter_hits.parquet 不在本機")
    import pandas as _pd
    from src.extract_lite import load_dataset
    hits = _pd.read_parquet(HITS)
    target = set(hits.loc[hits["rule"] == "需送分類器", "prompt_text_sha256"])
    d = load_dataset()
    d = d[d.prompt_text_sha256.isin(target) & d.prompt_text_len.notna()]
    m = d.prompt_tokens.fillna(0) > K * d.prompt_text_len
    assert int(m.sum()) == requests
    assert int(d.loc[m, "prompt_text_sha256"].nunique()) == contents
    text = _gap_section().replace("**", "")
    row = [l for l in text.splitlines()
           if l.lstrip().startswith(f"| {K} |")]
    assert len(row) == 1, f"找不到 K={K} 那一列"
    assert f"{requests:,}" in row[0] and f"{contents:,}" in row[0]


def test_協定記的_clean_側比例與實際相符():
    """clean 的 59.26% 是「這件事存在」的證據，不是 lite 的估計。

    協定必須同時記數字**與不可轉移**這句話——只記數字的話，下一個人會
    把它當成 lite 的比例用，而那個誤用不會有任何徵兆。
    """
    from src.schema import load_dataset as clean_ds
    d = clean_ds()
    ins = d.instructions_length.fillna(0) > 0
    mem = d.memory_len.fillna(0) > 0
    text = _gap_section().replace("**", "")
    for n, pct in ((int(ins.sum()), 100 * ins.mean()),
                   (int(mem.sum()), 100 * mem.mean()),
                   (int((ins | mem).sum()), 100 * (ins | mem).mean())):
        row = [l for l in text.splitlines()
               if l.lstrip().startswith("|") and f"| {n:,} |" in l]
        assert row, f"協定裡沒有 {n:,} 這一列"
        assert f"{pct:.2f}%" in row[0], f"{n:,} 那列的佔比應為 {pct:.2f}%：{row[0]}"
    assert "不可轉移到 lite" in text


def test_協定第一節不記項數():
    """手工維護的計數在本專案兩輪內錯過兩次。加一項就會再錯一次。"""
    intro = _section("## 一、凍結座標").split("###", 1)[0]
    import re
    assert not re.search(r"這[一二三四五六七八九十]+項一旦變動", intro)
    assert "不記數目" in intro


# --- 字典與 FIELD_HELP 的同步 -----------------------------------------------
#
# **本檔最重要的一組測試。** 兩邊漂掉不會報錯：判定器照樣跑、文件照樣讀得懂，
# 只是判定器用的是舊值域、文件寫的是新值域。而輸出檔上看不出任何差別——
# `prompt_sha256` 只證明「那一次用的是這個 FIELD_HELP」，不證明它與字典一致。
#
# 字典分兩層（見 `ref/label_dictionary.csv` 的〈層別說明〉）：
#   分類法    給人標註內容用的最終標籤，判定器看不到
#   分流判準  給判定器判群集去留用的，必須與 FIELD_HELP 逐字相同

DICT_CSV = REPO / "ref" / "label_dictionary.csv"


def _dict_rows():
    import csv as _csv
    return list(_csv.DictReader(DICT_CSV.read_text(encoding="utf-8-sig").splitlines()))


def _triage_rows():
    rows = [r for r in _dict_rows() if r["層"] == "分流判準"]
    # **空真的防線。** 沒有分流判準列時，下面每一個「逐項相同」都會自動成立。
    # 這一行讓「字典裡忘了寫」與「字典寫對了」分得開。
    assert rows, "字典裡沒有任何『分流判準』列——同步測試會變成空真"
    return rows


def test_字典的層欄只有三種值而且說明列在最前面():
    rows = _dict_rows()
    assert {r["層"] for r in rows} == {
        "（層別說明）", "（字典檔頭）", "分類法", "分流判準"}
    assert [r["層"] for r in rows[:2]] == ["（層別說明）", "（層別說明）"]
    assert {r["軸"] for r in rows[:2]} == {"分類法", "分流判準"}
    # 說明列要講出兩層的分別，不只是列出軸名
    text = " ".join(r["一句話定義"] + r["邊界說明"] for r in rows[:2])
    assert "判定器看不到" in text and "FIELD_HELP" in text


def test_domain_事前拆分規則在字典檔頭且八值判準處置全空():
    rows = _dict_rows()
    headers = [r for r in rows if r["層"] == "（字典檔頭）" and r["軸"] == "domain"]
    assert len(headers) == 1
    text = headers[0]["一句話定義"] + headers[0]["邊界說明"]
    for token in ("uid≥10", "600 筆的 30%", "180 筆", "低於 10 筆",
                  "2 組以上遮罩帳號", "重標全部 600 筆", "不可只重標該類"):
        assert token in text
    domain = [r for r in rows if r["層"] == "分類法" and r["軸"] == "domain"]
    assert len(domain) == 8
    assert all(r["判準"] == "" and r["處置"] == "" for r in domain)


def test_分類法那一層還是原本四個軸():
    """加了分流判準之後，內容分類的軸不該被動到。"""
    axes = {r["軸"] for r in _dict_rows() if r["層"] == "分類法"}
    assert axes == {"invocation", "role", "domain", "status"}


def test_分流判準的軸與_FIELD_HELP_相同():
    carrier = jc._load_carrier()
    assert {r["軸"] for r in _triage_rows()} == set(carrier.FIELDS)


@pytest.mark.parametrize("axis", ["frame_owner", "disposition", "axis_level"])
def test_分流判準的值域與_FIELD_HELP_逐項相同(axis):
    """**順序也要相同。** FIELD_HELP 是 dict，順序會進 `render_axes()`，
    因而進指紋；字典裡順序不同的話，讀字典的人看到的優先順序與模型看到的
    不一樣，而那個差別不會出現在任何輸出上。"""
    carrier = jc._load_carrier()
    assert axis in carrier.FIELD_HELP, f"FIELD_HELP 沒有 {axis}"
    got = [r["代碼"] for r in _triage_rows() if r["軸"] == axis]
    assert got == list(carrier.FIELD_HELP[axis]), f"{axis} 值域或順序不一致"


@pytest.mark.parametrize("axis", ["frame_owner", "disposition", "axis_level"])
def test_分流判準的定義文字與_FIELD_HELP_逐字相同(axis):
    """只比值域不夠：值域相同而定義漂掉，判定器與文件說的是兩件事，
    而一致率、分布、指紋全都照樣算得出來。"""
    carrier = jc._load_carrier()
    got = {r["代碼"]: r["一句話定義"] for r in _triage_rows() if r["軸"] == axis}
    assert got == dict(carrier.FIELD_HELP[axis]), f"{axis} 的定義文字不一致"


def test_同步測試抓得到單邊修改():
    """**突變測試內建。** 這組測試的價值全在「漂掉時會紅」，

    而那件事沒有正面證據——測試綠著的時候，看不出它是在保護什麼。
    這裡就地造一個漂掉的 FIELD_HELP，確認比對真的會失敗。
    """
    carrier = jc._load_carrier()
    drifted = {ax: dict(v) for ax, v in carrier.FIELD_HELP.items()}
    ax = "disposition"
    code = next(iter(drifted[ax]))
    drifted[ax][code] += "（單邊改了一個字）"
    got = {r["代碼"]: r["一句話定義"] for r in _triage_rows() if r["軸"] == ax}
    assert got != dict(drifted[ax])
    dropped = {k: v for k, v in drifted[ax].items() if k != code}
    assert list(got) != list(dropped)


# --- axis_level 新值域（2026-09-09，指紋 a2dc7b11…）--------------------------
#
# **這一組釘的是「值域改了但兩邊一起改」也要被看見。** 同步測試只保證
# 字典與 FIELD_HELP 一致；兩邊同時改成別的東西，同步測試照樣全綠。

AXIS_LEVEL_VALUES = ("all_three", "invocation_only", "not_attributable",
                     "insufficient_data", "none", "unsure")


def test_axis_level_的值域是五個分類值加一個棄權值():
    """**不要報成「六個值」。** `unsure` 與其他值不同層——那些是分類，
    這是棄權。把它算進分類值的數目，等於把棄權當成一種判定結果。"""
    carrier = jc._load_carrier()
    got = tuple(carrier.FIELDS["axis_level"])
    assert got == AXIS_LEVEL_VALUES
    分類值 = [v for v in got if v != "unsure"]
    assert len(分類值) == 5
    assert got[-1] == "unsure", "棄權值要排在最後，順序會進指紋"


def test_三軸的棄權值都是_unsure_而且只有一個():
    carrier = jc._load_carrier()
    for axis, values in carrier.FIELDS.items():
        assert values.count("unsure") == 1, axis
        assert values[-1] == "unsure", axis


@pytest.mark.parametrize("axis", ["frame_owner", "disposition", "axis_level"])
def test_同步測試抓得到單邊修改_逐軸(axis):
    """`test_同步測試抓得到單邊修改` 只突變 disposition。值域改版之後，
    新加的值若沒有被同一道保護涵蓋，漂掉時仍然會全綠——所以三軸各驗一次。

    對每個軸都挑**最後一個實質值**來突變，而不是第一個：新值通常加在
    中間或尾端，只突變第一個等於永遠在驗舊值。
    """
    carrier = jc._load_carrier()
    codes = [c for c in carrier.FIELD_HELP[axis] if c != "unsure"]
    victim = codes[-1]
    got = {r["代碼"]: r["一句話定義"] for r in _triage_rows() if r["軸"] == axis}
    drifted = dict(carrier.FIELD_HELP[axis])
    drifted[victim] += "（單邊改了一個字）"
    assert got != drifted, f"{axis}/{victim} 漂掉時比對仍然成立——保護是空的"
    dropped = {k: v for k, v in carrier.FIELD_HELP[axis].items() if k != victim}
    assert list(got) != list(dropped), f"{axis}/{victim} 被刪掉時值域比對仍然成立"


def test_三個判不出來的值把不可互相取代的理由寫死在字典裡():
    """`not_attributable`／`insufficient_data`／`none` 三者都是「群層判不出
    三軸」，但成因不同、處置不同、分母不同。合併會讓 `none` 的佔比混入
    非分類問題——而那個佔比正是要報的「分類器能力上限」。

    比照 `status` 的 `not_applicable` 那條的寫法：理由寫在檔案裡，
    不是寫在對話裡。
    """
    rows = {r["代碼"]: r for r in _triage_rows() if r["軸"] == "axis_level"}
    assert set(rows) == set(AXIS_LEVEL_VALUES)

    # 每一個都要指出它對應哪一個 disposition，否則共現檢查無從對起
    assert "service_relay" in rows["not_attributable"]["邊界說明"]
    assert "insufficient" in rows["insufficient_data"]["邊界說明"]

    # 成因要寫出來，不能只寫「不同」
    assert "不使用本 gateway 的第三方" in rows["not_attributable"]["邊界說明"]
    assert "上游 schema" in rows["insufficient_data"]["邊界說明"]

    # 「不可互相取代」要三個值都講到，且要出現在同一格裡——
    # 拆成三格各講一句的話，讀任何一格都看不出三者的關係
    for code in ("not_attributable", "insufficient_data", "none"):
        note = rows[code]["邊界說明"]
        assert "三者不可互相取代" in note, code
        assert all(v in note for v in
                   ("not_attributable", "insufficient_data", "none")), code

    # none 是訊號不是預設值，而且要把不確定的案例導向 unsure
    assert "不是一個安全的預設值" in rows["none"]["邊界說明"]
    assert "unsure" in rows["none"]["邊界說明"]

    # 殘留撞號要留在檔案裡，不是留在對話裡
    assert "tool_injected" in rows["none"]["邊界說明"]


def test_棄權值的保留理由寫在字典裡():
    """移除 `unsure` 會讓模型被迫在分類值裡挑一個，而挑出來的那個
    在輸出檔上與有把握的判定**長得一模一樣**。理由要寫死，否則下一個
    看到「有一個值幾乎沒人用」的人會把它刪掉。"""
    rows = [r for r in _triage_rows() if r["代碼"] == "unsure"]
    assert len(rows) == 3, "三個軸各要有一列 unsure"
    for r in rows:
        note = r["邊界說明"]
        assert "棄權" in note, r["軸"]
        assert "被迫" in note, r["軸"]
        # 值域大小的講法要分開寫，不可寫成「N 個值」
        assert "個分類值 + 一個棄權值" in note, r["軸"]
        # 同步契約：這一格必須與 FIELD_HELP 的 "" 相同
        assert r["一句話定義"] == "", r["軸"]
# --- 協定第三節：兩軸的共現檢查 ---------------------------------------------
#
# **這一節是驗收條件，不是測試。** 這裡的測試只保證條件本身寫得完整、
# 而且接得回字典——真正的檢查要等 118 群跑完才做得了。
#
# 會壞而沒有徵兆的地方有三個：門檻寫成單向、n 的下限漏掉、
# 兩軸對應的值在字典改名之後沒有跟著改。

COOCCUR = "### 兩軸的共現檢查（118 群跑完之後的驗收條件）"


def _cooccur_section() -> str:
    text = PROTOCOL.read_text(encoding="utf-8")
    assert text.count(COOCCUR) == 1, "共現檢查那一節不見了或重複"
    return text.split(COOCCUR)[1].split("\n### ")[0]


def test_共現檢查的兩對值都接得回字典():
    """**協定寫的值名要與字典逐字相同。** 字典改名而協定沒改，
    驗收時會拿一個不存在的值去查，查出來永遠是 0 群——而 0 群看起來
    像「這一格沒人用」，不像「名字錯了」。
    """
    sec = _cooccur_section()
    carrier = jc._load_carrier()
    for dispo, level in (("insufficient", "insufficient_data"),
                         ("service_relay", "not_attributable")):
        assert dispo in carrier.FIELDS["disposition"], dispo
        assert level in carrier.FIELDS["axis_level"], level
        assert dispo in sec and level in sec, (dispo, level)


def test_共現檢查兩個方向都寫了而且標明分母不同():
    """只報一個方向會把另一個方向的錯誤蓋掉：召回高而精確低，
    代表 disposition 那一格在濫用實質分類，但召回那個數字看起來很好。

    兩個方向的分母不同，所以兩個百分比不得並排而不標明——
    見〈並排的數字要標明各自的算法〉。
    """
    sec = _cooccur_section()
    assert "P(axis_level = X | disposition = Y)" in sec
    assert "P(disposition = Y | axis_level = X)" in sec
    assert "分母不同" in sec
    assert "並排的數字要標明各自的算法" in sec


def test_共現檢查有_n_的下限而且下限講的是解析度():
    """**門檻沒有 n 的下限就是空的。** n=5 時一群 = 20pp，
    任何百分比門檻在那裡都讀不出來，而百分比照樣算得出來。
    """
    sec = _cooccur_section()
    assert "< 10" in sec, "n 的下限不見了"
    assert "不算百分比" in sec and "原始計數" in sec
    assert "差小於量測解析度時，不要從方向讀出結論" in sec


def test_共現檢查的門檻已裁定而且三段都有數字():
    """門檻是預先登記的。**跑完再挑一個剛好通過的數字，等於用結果定義通過。**

    定案之後這裡改驗「不再標著待裁定」——留著「建議值」的字樣，
    下一個讀的人會以為還可以動它。
    """
    sec = _cooccur_section()
    assert "已裁定" in sec
    assert "建議值" not in sec and "待裁定" not in sec, "門檻已定案，字樣要拿掉"
    assert "寫定於重跑之前" in sec
    for token in ("≥ 95%", "90–95%", "< 90%"):
        assert token in sec, token
    # 定案之後唯一能改的理由要寫出來，否則「不能改」讀起來像不能質疑
    assert "用結果定義通過" in sec


def test_門檻不得因為這一批沒過而放寬():
    """**這條擋的是日後有人拿 82.6% 把門檻降到 85%。**

    雜訊底線 82.6% 是「兩次呼叫之間」的量測，共現檢查的兩格出自同一次
    回覆——噪聲來源不同，前者不是後者的下界。這段理由要逐字留著：
    它是唯一擋得住「85% 也很嚴格啊」的東西。
    """
    sec = _cooccur_section()
    assert "82.6%" in sec
    assert "兩次呼叫之間" in sec
    assert "同一次" in sec
    assert "不可並排" in sec
    assert "不能因為「這一批剛好沒過」而改" in sec


def test_共現檢查沒有把_82_6_當成這裡的下界():
    """**82.6% 是兩次呼叫之間的雜訊，共現檢查的兩格出自同一次回覆。**
    兩者噪聲來源不同，把前者當成後者的下界會把門檻訂得太鬆——
    而訂鬆之後，判定不一致會安靜地通過驗收。

    這一條釘的是「協定明寫了不可並排」，不是「協定沒提到 82.6%」。
    """
    sec = _cooccur_section()
    assert "82.6%" in sec, "沒有提到雜訊底線，讀的人會自己拿它來當下界"
    assert "同一次" in sec
    assert "不可並排" in sec


def test_共現檢查說明了不通過要改定義而不是重跑():
    """重跑同一組定義只會得到同一組不一致——兩格來自同一次回覆，
    不一致的來源是定義，不是抽樣波動。少了這一句，第一個反應會是重跑。
    """
    sec = _cooccur_section()
    assert "不是重跑，是改定義" in sec
    assert "全量重跑" in sec        # 改完定義之後要做的事也要寫


def test_none_的佔比要先扣掉_tool_injected():
    """**殘留撞號要寫在協定裡，不只寫在字典裡。** 讀 `none` 佔比的人
    看的是協定；字典裡那一句他不會翻到，而 `none` 的佔比正是要報的數字。
    """
    sec = _cooccur_section()
    assert "tool_injected" in sec
    assert "扣除" in sec or "扣掉" in sec
    # 字典那一邊也要有，兩邊各自都會被單獨讀到
    rows = {r["代碼"]: r for r in _triage_rows() if r["軸"] == "axis_level"}
    assert "tool_injected" in rows["none"]["邊界說明"]
def test_三條_must_全部由_AXIS_CONSTRAINTS_自動檢查():
    """新增 must 不需要在判定器再加一個 disposition 特例。"""
    carrier = jc._load_carrier()
    must = [c for c in carrier.AXIS_CONSTRAINTS if c[2] == "must"]
    assert len(must) == 3
    for disposition, expected, _, _ in must:
        assert jc.must_constraint_violation(
            disposition, expected, carrier.AXIS_CONSTRAINTS) is None
        for actual in carrier.FIELDS["axis_level"]:
            violation = jc.must_constraint_violation(
                disposition, actual, carrier.AXIS_CONSTRAINTS)
            if actual == expected:
                assert violation is None
            else:
                assert violation == (disposition, expected, actual)


def test_unsure_不算符合_must():
    for disposition, expected, strength, _ in _constraints():
        if strength == "must":
            assert expected != "unsure"
            assert jc.must_constraint_violation(
                disposition, "unsure", _constraints()) == (
                    disposition, expected, "unsure")


def test_default_偏離不算_must_違反():
    carrier = jc._load_carrier()
    for disposition, _, strength, _ in carrier.AXIS_CONSTRAINTS:
        if strength == "default":
            for actual in carrier.FIELDS["axis_level"]:
                assert jc.must_constraint_violation(
                    disposition, actual, carrier.AXIS_CONSTRAINTS) is None


def test_新增_must_不必改檢查函式():
    synthetic = (("new_disposition", "new_level", "must", "fixture"),)
    assert jc.must_constraint_violation(
        "new_disposition", "wrong", synthetic) == (
            "new_disposition", "new_level", "wrong")
    assert jc.must_constraint_violation(
        "new_disposition", "new_level", synthetic) is None


def test_must_機械檢查不硬編任何_disposition():
    src = Path(jc.__file__).read_text(encoding="utf-8")
    fn = src.split("def must_constraint_violation(")[1].split("\ndef ")[0]
    carrier = jc._load_carrier()
    # unsure 只出現在 docstring 的「不是例外」說明；函式本體沒有拿它特判。
    for disposition in carrier.FIELDS["disposition"]:
        if disposition == "unsure":
            continue
        assert disposition not in fn


def test_異常檢查不進提示詞():
    """**加一道檢查不該變成換一個實驗。** 這條檢查若碰到指紋的任何一項
    輸入，既有判定就全部作廢——而它只是輸出端的一個計數。
    """
    src = jc.__file__ and Path(jc.__file__).read_text(encoding="utf-8")
    fn = src.split("def must_constraint_violation(")[1].split("\ndef ")[0]
    for forbidden in ("PROMPT_TEMPLATE", "FIELD_HELP", "META_FIELDS",
                      "BLIND_PROBE", "cli_flags", "build_json_schema"):
        assert forbidden not in fn, forbidden
    # 指紋本身也釘住：CURRENT_FINGERPRINT 沒變就代表沒動到那十一項
    assert _real_fingerprint() == CURRENT_FINGERPRINT


def test_約束值都能由_json_schema_表達():
    carrier = jc._load_carrier()
    schema = jc.build_json_schema(carrier.FIELDS)
    assert jc.assert_constraints_representable(
        carrier.AXIS_CONSTRAINTS, schema) is None
    dispositions = schema["properties"]["disposition"]["enum"]
    levels = schema["properties"]["axis_level"]["enum"]
    for disposition, level, _, _ in carrier.AXIS_CONSTRAINTS:
        assert disposition in dispositions
        assert level in levels


def test_main_使用同一份約束做_schema_與輸出檢查():
    src = Path(jc.__file__).read_text(encoding="utf-8")
    main = src.split("def main(", 1)[1]
    assert "assert_constraints_representable(carrier.AXIS_CONSTRAINTS, schema)" in main
    assert "must_constraint_violation(" in main
    assert "carrier.AXIS_CONSTRAINTS" in main


def test_協定寫了_tool_injected_不加值的理由():
    """**「不加值」是決定，理由不寫下來就會被當成疏漏補回去。**
    下一個看到 axis_level 沒有 not_applicable 的人，第一個念頭是加一個。
    """
    sec = _cooccur_section()
    assert "整群移出分類母數" in sec
    assert "那個問題**不成立**" in sec
    assert "不需要判" in sec
    # 不加 not_applicable 的理由：混合軸
    assert "not_applicable" in sec
    assert "適用性" in sec and "能力" in sec
    # 驗收檢查是回報不是硬擋，理由也要在
    assert "不硬擋" in sec
    assert "不換指紋" in sec


def test_扣除規則同時要求報被扣掉的群數():
    """**只寫「要扣掉」不夠。** 分母悄悄變小，比例照樣算得出來，
    而讀者無從判斷那個扣除動了多少。
    """
    sec = _cooccur_section()
    assert "扣除 `tool_injected` 後的\nnone" in sec or "扣除 `tool_injected` 後的" in sec
    assert "同時報被扣掉的群數" in sec
    # 字典那一列也要有同一條——讀佔比的人看協定，改值域的人看字典
    rows = {r["代碼"]: r for r in _triage_rows() if r["軸"] == "axis_level"}
    note = rows["none"]["邊界說明"]
    assert "整群移出分類母數" in note
    assert "不需要判" in note
    assert "not_applicable" in note and "混合軸" in note
    assert "同時報被扣掉的群數" in note
    assert "未解決" not in note, "已裁定，不可再標成未解決"
# --- 軸間約束（2026-09-10，指紋 83086cd2…）----------------------------------
#
# **這一組最重要的是最後一個測試。** 前面幾個驗的是「約束寫對了」，
# 最後一個驗的是「main() 真的用了它」——而那正是本輪查出來的漏洞形狀。

def _constraints():
    return jc._load_carrier().AXIS_CONSTRAINTS


def test_軸間約束的每個值都在值域裡():
    """**約束寫錯值不會報錯。** 打錯字的那條就永遠不會成立，
    而輸出看起來完全正常——模型照樣填、共現率照樣算得出來。
    """
    carrier = jc._load_carrier()
    assert _constraints(), "沒有任何約束——下面每一條都會變成空真"
    for dispo, level, strength, why in _constraints():
        assert dispo in carrier.FIELDS["disposition"], dispo
        assert level in carrier.FIELDS["axis_level"], level
        assert strength in ("must", "default"), strength
        assert why.strip(), f"{dispo} 沒寫理由"


def test_五個_disposition_分類值都有約束():
    """`unsure` 是棄權值，不該有約束；其餘五個都要有，且各只有一條。"""
    carrier = jc._load_carrier()
    分類值 = [v for v in carrier.FIELDS["disposition"] if v != "unsure"]
    covered = [c[0] for c in _constraints()]
    assert sorted(covered) == sorted(分類值), (sorted(covered), sorted(分類值))
    assert len(covered) == len(set(covered)), "同一個 disposition 有兩條約束"
    assert "unsure" not in covered


def test_must_與_default_要分開而且不能全部是_must():
    """**全部寫成 must，axis_level 就變成 disposition 的純函數。**
    那一軸的資訊量歸零，而共現檢查會退化成「模型有沒有照抄規則」——
    它量的就不再是兩個判斷是否一致。
    """
    strengths = {c[0]: c[2] for c in _constraints()}
    must = {k for k, v in strengths.items() if v == "must"}
    default = {k for k, v in strengths.items() if v == "default"}
    assert must and default, "must 與 default 至少各要有一個"
    # 定義上必然的那三條
    assert must == {"tool_injected", "service_relay", "insufficient"}, must
    assert default == {"batch_project", "user_envelope"}, default


def test_tool_injected_的約束就是本輪要修的那一條():
    c = {x[0]: x for x in _constraints()}["tool_injected"]
    assert c[1] == "none"
    assert c[2] == "must"
    assert "移出分類母數" in c[3]
    assert "不成立" in c[3]


def test_約束與字典的處置欄不相牴觸():
    """字典是給人讀的、約束是給模型讀的，兩邊講的必須是同一件事。

    **不比對逐字**——處置欄還寫了記帳規則（「計入不可判定率」）之類
    判定器不需要的東西。只比對「字典有沒有提到那個 axis_level 值」。
    """
    rows = {r["代碼"]: r for r in _triage_rows() if r["軸"] == "axis_level"}
    for dispo, level, _, _ in _constraints():
        assert level in rows, level
        note = rows[level]["邊界說明"]
        assert dispo in note, f"字典的 axis_level={level} 沒提到 {dispo}"


def test_約束有進提示詞的_axes_block():
    """**只加常數不算數。** 常數在載具裡而 `render_axes` 沒帶它，
    提示詞就沒有約束，而指紋、輸出、測試全都看不出差別。
    """
    carrier = jc._load_carrier()
    無 = jc.render_axes(carrier.FIELDS, carrier.FIELD_HELP)
    有 = jc.render_axes(carrier.FIELDS, carrier.FIELD_HELP,
                        carrier.AXIS_CONSTRAINTS)
    assert 無 != 有, "帶了約束卻沒有改變 axes_block"
    assert "軸間約束" in 有 and "軸間約束" not in 無
    assert "disposition=tool_injected → axis_level=none" in 有
    assert "必須" in 有 and "預設" in 有
    assert jc.template_fingerprint(無) != jc.template_fingerprint(有)


def test_探針把不算的東西列成清單而不是附註():
    """舊問法的偽陽性率 58%，成因是例外寫成一句要模型自己界定的附註。

    這裡驗的是**結構**：兩份清單都在、而且「只有不算的東西 → NO」
    這個對應寫死了。不驗字數。
    """
    probe = jc.BLIND_PROBE_PROMPT
    assert "【不算】" in probe and "【算】" in probe
    # 不算的兩項要逐項列出
    assert "系統提示本身" in probe
    assert "當前日期" in probe
    assert "即使它指出使用者是誰，也不算" in probe
    # 算的四項
    for token in ("CLAUDE.md", "先前的對話紀錄", "檔案系統"):
        assert token in probe, token
    # 第 (4) 項曾被「環境概述提到 shell」誤判過
    assert "實際可呼叫的工具定義為準" in probe
    # NO 的條件要明寫
    assert "→ answer 回 NO" in probe
    assert probe.count("YES") >= 1 and probe.count("NO") >= 1


def test_main_實際算出來的指紋就是釘住的那個(monkeypatch, capsys, tmp_path):
    """**本輪查出來的漏洞就是這個形狀。**

    `_real_fingerprint()` 是測試自己重建的一份，而重建的東西會與被重建的
    東西漂掉——本輪的盲化探針量測就是這樣量錯的（旗標串用
    `startswith("--json-schema")` 過濾，只濾掉旗標名沒濾掉它的值）。

    所以這裡不重建：跑 `main(["--dry-run"])`，抓它**自己印出來**的那一行。
    """
    out = tmp_path / "fp_probe.csv"
    assert jc.main(["--dry-run", "--output", str(out)]) == 0
    printed = [ln for ln in capsys.readouterr().out.splitlines()
               if "提示詞指紋" in ln]
    assert len(printed) == 1, printed
    assert CURRENT_FINGERPRINT in printed[0], printed[0]
# --- 隱藏上下文欄與盲化門檻（2026-09-10，指紋 e13f9738…）--------------------

def test_insufficient_的判準所需欄位有送進判定器():
    """**判準寫了但判定器收不到對應欄位，等於沒寫。**

    `insufficient` 的判準是 `prompt_tokens > 4 × prompt_text_len`，
    而 `META_FIELDS` 一直沒有任何與 token 有關的欄位——實測兩次全量執行
    `insufficient` 都是 0 群。這一條釘的是那個欄位還在。
    """
    assert "隱藏上下文請求佔比" in jc.META_FIELDS
    rows = {r["代碼"]: r for r in _triage_rows() if r["軸"] == "disposition"}
    assert "prompt_tokens" in rows["insufficient"]["判準"]


def test_中繼資料欄位在群集檔裡都存在():
    """`render_metadata` 逐欄取值，缺一欄就 KeyError——而那只有在真的跑
    判定的時候才會炸，`--dry-run` 不會碰到。"""
    if not CLUSTERS.exists():
        pytest.skip("群集檔不在本機")
    cols = set(pd.read_parquet(CLUSTERS).columns)
    missing = [k for k in jc.META_FIELDS if k not in cols]
    assert not missing, f"clusters.parquet 缺 {missing}"


def test_盲化門檻落在實測的兩個值之間():
    """**這條現在是真判準，不是控制界。**

    量法：同一群（A113）、同一份真實判定提示詞，有無 `--system-prompt`
    各跑一次。未盲化 3890（n=3 零抖動）、盲化 2596。門檻要落在中間，
    而且兩側都要有餘裕——舊值 2600 只離盲化側 4 tokens，任何提示詞加長
    都會讓它變成常態警示，而常態化的警示等於沒有警示。
    """
    lo = jc.PROMPT_TOKENS_BLINDED_MEASURED
    hi = jc.PROMPT_TOKENS_UNBLINDED_MEASURED
    assert lo < jc.PROMPT_TOKENS_FLOOR_WARN < hi
    margin = min(jc.PROMPT_TOKENS_FLOOR_WARN - lo,
                 hi - jc.PROMPT_TOKENS_FLOOR_WARN)
    assert margin >= 500, f"兩側餘裕只有 {margin} tokens，太靠邊"
    # 門檻不再等於單次警示線——兩者管的是不同的事
    assert jc.PROMPT_TOKENS_FLOOR_WARN != jc.PROMPT_TOKENS_WARN


def test_盲化判定用的是門檻不是等號():
    """邊界要驗，否則「剛好等於」落在哪一邊沒有人知道。"""
    t = jc.PROMPT_TOKENS_FLOOR_WARN
    assert not jc.prompt_tokens_floor_broken(t)
    assert not jc.prompt_tokens_floor_broken(t - 1)
    assert jc.prompt_tokens_floor_broken(t + 1)
    # 實測的兩個值要分別落在對的一邊
    assert not jc.prompt_tokens_floor_broken(jc.PROMPT_TOKENS_BLINDED_MEASURED)
    assert jc.prompt_tokens_floor_broken(jc.PROMPT_TOKENS_UNBLINDED_MEASURED)


def test_協定把_must_與_default_的檢查分開():
    """**放在同一條規則底下，量到的東西會混成兩種不同的性質。**
    must 那三條量的是指令遵循（模型被明文指示這樣填），
    default 那兩條才是一致性。共現率 100% 在前者不代表任何事。
    """
    sec = _cooccur_section()
    assert "指令遵循" in sec and "一致性" in sec
    assert "違反即異常" in sec
    assert "`unsure` **不算符合**" in sec
    assert "不算百分比" in sec
    # 違反的意思要寫明：提示詞問題，不是判定問題
    assert "提示詞的問題" in sec
    # 三段門檻那張表要標明只適用 default
    assert "只適用 default" in sec
    # 為什麼只有三條 must 的理由要在
    assert "純函數" in sec and "資訊量歸零" in sec


def test_協定列的_must_與_default_與載具一致():
    """協定寫一份、載具寫一份，兩邊漂掉不會報錯——協定寫著五條約束，
    判定器只送三條，而輸出看不出差別。"""
    sec = _cooccur_section()
    for dispo, level, strength, _ in jc._load_carrier().AXIS_CONSTRAINTS:
        assert f"`{dispo}` → `{level}`" in sec, (dispo, level)
        # 該列要標對級別
        line = [ln for ln in sec.splitlines()
                if f"`{dispo}` → `{level}`" in ln]
        assert len(line) == 1, (dispo, line)
        assert f"**{strength}**" in line[0], (dispo, strength, line[0])



def test_用量信封回_0_不算盲化良好():
    """**0 不是一個很小的值，是沒量到。**

    實測 118 群裡有 1 群（B082）的用量信封整組回 0——呼叫成功、成本
    0.0163、耗時 18 秒，但三個 token 欄位全是 0。`min()` 因此取到 0，
    而 `prompt_tokens_floor_broken(0)` 是 False：**地板檢查靜默通過**，
    那正是它要擋的事情的反面。

    這一條釘的是「0 落在安全側」這個事實本身——修法（收尾時把 0 排除並
    另外計數）在 main() 裡，這裡先把危險性寫死，免得有人把它讀成
    「0 代表很盲」。
    """
    assert not jc.prompt_tokens_floor_broken(0), \
        "若 0 變成 broken，下面那句就不再是這條測試要防的事"
    # 真正的盲化值與 0 差了兩千多，0 顯然不是一個合法的量測結果
    assert jc.PROMPT_TOKENS_BLINDED_MEASURED > 2000
    src = Path(jc.__file__).read_text(encoding="utf-8")
    assert "token_unmeasured" in src, "沒量到的次數要單獨計數"
    assert "ptt <= 0" in src, "0 要在累計最小值之前被排除"


def test_缺值那條記了_0_落在安全側這個形狀():
    """條目的價值全在「檢查的結果與它要擋的事情相反」那一句。
    少了它，這條會被讀成一般的資料清理建議。"""
    text = (REPO / "docs" / "ENGINEERING_NOTES.md").read_text(encoding="utf-8")
    entry = _note_body("缺值不要當成一個極端值參與比較")
    assert "正好相反" in entry
    assert "排除並計數" in entry
    # 與既有的 NA/0 原則要接上，否則看不出這是同一個錯誤換了地方
    assert "unpriced_local" in entry and "unpriced_no_table" in entry
    assert "檢查邏輯" in entry
    # 單邊門檻那個推論不可省——它才是可執行的部分
    assert "安全側" in entry


def test_閘門那條要求把指名記下來():
    """指名會漂就是誤報——這是唯一不必重跑就能分辨誤報與真訊號的辦法。"""
    entry = _note_body("閘門的問句若含需要判斷的例外條款，判斷本身會成為誤報來源")
    assert "指名是哪一項" in entry
    assert "理由會漂就是誤報" in entry
    assert "留一個" in entry and "欄位" in entry
