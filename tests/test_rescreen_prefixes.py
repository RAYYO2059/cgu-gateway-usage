"""`_rescued_scratchpad/rescreen_prefixes.py` 的測試。

**全部假資料，不執行重篩、不碰真實內容。** 重篩本身由專案主人在終端執行。

本檔最重要的一組是盲性：重篩若看得到第一次的答案，量到的就是「記不記得
上次怎麼判」，而那個數字看起來與真正的一致率完全一樣，事後也分不出來。

腳本在 repo 外（換機器時要一起帶），找不到就 skip 而不是 fail。

跑法：pytest tests/test_rescreen_prefixes.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO.parent / "_rescued_scratchpad" / "rescreen_prefixes.py"


def _load():
    if not SCRIPT.exists():
        pytest.skip(f"重篩腳本不在本機（{SCRIPT}），跳過")
    spec = importlib.util.spec_from_file_location("rescreen_prefixes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rescreen_prefixes"] = module
    spec.loader.exec_module(module)
    return module


rs = _load()


def _screen(rows):
    """假的第一次篩檢結果。(group_id, personal_data, elapsed_sec)"""
    return pd.DataFrame(
        [{"group_id": g, "personal_data": p, "note": "",
          "elapsed_sec": e, "screened_at": "t"} for g, p, e in rows])


# --- 分層 -------------------------------------------------------------------
@pytest.mark.parametrize("seconds,layer", [
    (0.0, "<=1s"), (0.6, "<=1s"), (1.0, "<=1s"),
    (1.1, "1-10s"), (5.0, "1-10s"), (10.0, "1-10s"),
    (10.1, ">10s"), (123.8, ">10s"),
])
def test_分層的邊界(seconds, layer):
    assert rs.layer_of(seconds) == layer


def test_邊界一律歸下層():
    # 1.0 秒算「<=1s」不算「1-10s」。邊界含糊會讓兩次分層不同，
    # 而分層是逐層一致率的分母。
    assert rs.layer_of(1.0) == "<=1s"
    assert rs.layer_of(10.0) == "1-10s"


def test_負耗時會_raise():
    with pytest.raises(ValueError):
        rs.layer_of(-0.1)


# --- 名額分配 ---------------------------------------------------------------
def test_按比例分配且總數正確():
    # 真實的層大小：48 / 148 / 30，抽 20。
    got = rs.allocate({"<=1s": 48, "1-10s": 148, ">10s": 30}, 20)
    assert sum(got.values()) == 20
    assert got["1-10s"] > got["<=1s"] > got[">10s"]


def test_非空的層至少配一個():
    """比例分配會讓小層拿到 0，而小層往往正是要驗的那層。"""
    got = rs.allocate({"<=1s": 1, "1-10s": 200, ">10s": 2}, 20)
    assert got["<=1s"] >= 1
    assert got[">10s"] >= 1
    assert sum(got.values()) == 20


def test_不會配超過該層的母體數():
    got = rs.allocate({"<=1s": 2, "1-10s": 3, ">10s": 1}, 20)
    assert got["<=1s"] <= 2 and got["1-10s"] <= 3 and got[">10s"] <= 1
    assert sum(got.values()) == 6          # 母體只有 6，抽不到 20


def test_空的層配零():
    got = rs.allocate({"<=1s": 0, "1-10s": 10, ">10s": 0}, 5)
    assert got["<=1s"] == 0 and got[">10s"] == 0 and got["1-10s"] == 5


def test_分配是決定性的():
    a = rs.allocate({"<=1s": 48, "1-10s": 148, ">10s": 30}, 20)
    b = rs.allocate({"<=1s": 48, "1-10s": 148, ">10s": 30}, 20)
    assert a == b


# --- 抽樣 -------------------------------------------------------------------
def _big_screen():
    rows = []
    for i in range(48):
        rows.append((f"F{i:03d}", "clean", 0.8))
    for i in range(148):
        rows.append((f"M{i:03d}", "clean", 3.0))
    for i in range(30):
        rows.append((f"S{i:03d}", "clean", 40.0))
    return _screen(rows)


def test_抽樣每層都有樣本():
    """快的那批正是要驗的對象，不能讓隨機抽樣把它們漏掉。"""
    picks = rs.stratified_sample(_big_screen(), n=20)
    assert set(picks) == {"<=1s", "1-10s", ">10s"}
    for layer, ids in picks.items():
        assert len(ids) >= 1, layer
    assert sum(len(v) for v in picks.values()) == 20


def test_抽樣是決定性的():
    a = rs.stratified_sample(_big_screen(), n=20)
    b = rs.stratified_sample(_big_screen(), n=20)
    assert a == b


def test_換種子會抽到不同的群():
    a = rs.stratified_sample(_big_screen(), n=20, seed=1)
    b = rs.stratified_sample(_big_screen(), n=20, seed=2)
    assert a != b


def test_抽到的群確實屬於該層():
    picks = rs.stratified_sample(_big_screen(), n=20)
    assert all(g.startswith("F") for g in picks["<=1s"])
    assert all(g.startswith("M") for g in picks["1-10s"])
    assert all(g.startswith("S") for g in picks[">10s"])


def test_抽樣的回傳值只有_group_id():
    """耗時不得跟著流到顯示路徑上。"""
    picks = rs.stratified_sample(_big_screen(), n=20)
    for ids in picks.values():
        assert all(isinstance(g, str) for g in ids)


def test_種子寫死在程式裡而且會被印出來():
    assert isinstance(rs.SEED, int)
    source = SCRIPT.read_text(encoding="utf-8")
    assert "SEED = " in source
    assert "種子 SEED = {SEED}" in source or "SEED = {SEED}" in source


# --- 盲性 -------------------------------------------------------------------
# 這是本檔最重要的一組。
@pytest.mark.parametrize("bad", ["personal_data", "note", "elapsed_sec",
                                 "screened_at", "PERSONAL_DATA",
                                 "frame_owner", "confidence", "reason"])
def test_顯示路徑帶著第一次的答案會被硬擋(bad):
    with pytest.raises(AssertionError):
        rs.assert_blind(["group_id", "相異內容數", bad])


def test_乾淨的欄位清單通過():
    rs.assert_blind(["group_id", "source", "L層級", "相異內容數", "請求數",
                     "共同前綴長度_原始", "uid數", "代表_sha256"])


def test_顯示函式的輸出不含第一次的答案(capsys, monkeypatch):
    """把一列 clusters 送進顯示路徑，捕捉 stdout，確認第一次的答案沒出現。

    fixture 全是這裡現造的字串，不是真實內容。
    """
    carrier = rs._load_carrier()
    row = pd.Series({
        "group_id": "A001", "source": "a", "L層級": 30,
        "相異內容數": 974, "請求數": 7503,
        "共同前綴長度_原始": 35, "共同前綴長度_正規化": 34,
        "前綴佔中位內容長度比例": 0.1, "uid數": 182,
        "內容長度_min": 78, "內容長度_中位": 350, "內容長度_max": 28294,
        "unit_type分布": "{}", "長度分位分布": "{}", "provider分布": "{}",
        "request_style分布": "{}", "model_returned_top5": "{}",
        "代表_sha256": "0" * 64, "代表_source_path": "fake/path.json",
    })
    prefix_row = pd.Series({
        "group_id": "A001",
        "prefix_raw": "FRAME-AAA 這是現造的假前綴，不是任何真實內容。",
        "prefix_normalized": "FRAME-AAA 這是現造的假前綴，不是任何真實內容。",
        "prefix_len_raw": 30, "prefix_len_normalized": 30, "truncated": False,
    })
    rs.assert_blind(row.index)
    rs.assert_blind(prefix_row.index)
    carrier.render_screen(row, prefix_row)
    out = capsys.readouterr().out
    # 第一次的答案不得出現在畫面上
    for token in ("clean", "flagged", "unsure", "personal_data",
                  "elapsed_sec", "screened_at"):
        assert token not in out, f"畫面上出現了 {token}"


def test_重篩不從第一次的結果讀答案():
    """`personal_data` 只准出現在「寫自己的答案」那一處。

    讀第一次的耗時做分層、印各層母體數，都是允許的——那是抽樣的事，
    畫面上只會出現「<=1s 母體 48 群」這種計數，不會出現任何一群的答案。
    """
    import re
    source = SCRIPT.read_text(encoding="utf-8")
    body = source.split("def run_rescreen(", 1)[1].split("def run_compare(", 1)[0]
    # 第一次的結果只准被取 elapsed_sec（分層用），不准取答案
    subscripts = set(re.findall(r'\bscreen\["([^"]+)"\]', body))
    assert subscripts <= {"elapsed_sec", "group_id"}, subscripts
    assert "merge" not in body, "merge 會把第一次的答案帶進顯示路徑"


def test_重篩不讀第一次的答案欄():
    source = SCRIPT.read_text(encoding="utf-8")
    run = source.split("def run_rescreen(", 1)[1].split("def run_compare(", 1)[0]
    assert "personal_data" in run     # 只在寫自己的答案時出現
    assert 'a.loc[gid, "personal_data"]' not in run
    assert "merge" not in run, "merge 會把第一次的答案帶進顯示路徑"


# --- 比對 -------------------------------------------------------------------
def _pairs(rows):
    """(group_id, 第一次, 第二次, 第一次耗時)"""
    first = _screen([(g, a, e) for g, a, _, e in rows])
    second = _screen([(g, b, 1.0) for g, _, b, _ in rows])
    return first, second


def test_全部一致時一致率為一():
    first, second = _pairs([("A", "clean", "clean", 0.5),
                            ("B", "clean", "clean", 5.0),
                            ("C", "flagged", "flagged", 40.0)])
    got = rs.compare(first, second)
    assert got["整體一致率"] == 1.0
    assert got["不一致"] == []
    assert got["方向"] == {}


def test_不一致會逐項報出來():
    first, second = _pairs([("A", "clean", "flagged", 0.5),
                            ("B", "flagged", "clean", 5.0),
                            ("C", "clean", "clean", 40.0)])
    got = rs.compare(first, second)
    assert got["n"] == 3
    assert got["整體一致率"] == pytest.approx(1 / 3)
    assert got["不一致"] == ["A", "B"]
    assert got["方向"] == {"clean→flagged": 1, "flagged→clean": 1}


def test_逐層分開報():
    """high 與 low 是兩個不同的問題，混在一起算沒有診斷力。"""
    first, second = _pairs([("A", "clean", "flagged", 0.5),
                            ("B", "clean", "clean", 0.6),
                            ("C", "clean", "clean", 5.0),
                            ("D", "clean", "clean", 40.0)])
    got = rs.compare(first, second)
    assert got["逐層"]["<=1s"]["n"] == 2
    assert got["逐層"]["<=1s"]["一致率"] == 0.5
    assert got["逐層"]["1-10s"]["一致率"] == 1.0
    assert got["逐層"][">10s"]["一致率"] == 1.0


def test_層用的是第一次的耗時():
    # 第二次全部 1.0 秒；分層仍應依第一次的 40 秒歸到 >10s。
    first, second = _pairs([("A", "clean", "clean", 40.0)])
    got = rs.compare(first, second)
    assert got["逐層"][">10s"]["n"] == 1
    assert got["逐層"]["<=1s"]["n"] == 0


def test_只重篩一部分時只比對共同的群():
    first = _screen([("A", "clean", 1.0), ("B", "clean", 1.0),
                     ("C", "clean", 1.0)])
    second = _screen([("B", "clean", 1.0)])
    got = rs.compare(first, second)
    assert got["n"] == 1


def test_第二份多出來的群會被指出來():
    first = _screen([("A", "clean", 1.0)])
    second = _screen([("A", "clean", 1.0), ("Z", "clean", 1.0)])
    got = rs.compare(first, second)
    assert got["只在第二份裡的群"] == ["Z"]


def test_沒有共同的群時不炸掉():
    got = rs.compare(_screen([("A", "clean", 1.0)]),
                     _screen([("Z", "clean", 1.0)]))
    assert got["n"] == 0
    assert got["整體一致率"] is None


def test_比對不印任何前綴():
    source = SCRIPT.read_text(encoding="utf-8")
    body = source.split("def run_compare(", 1)[1].split("\ndef main(", 1)[0]
    for token in ("prefix_raw", "prefix_normalized", "PREFIXES"):
        assert token not in body, f"比對路徑碰到了 {token}"


# --- 沿用同一段程式 ---------------------------------------------------------
def test_顯示與作答沿用載具而不是複製一份():
    """兩次必須看到一樣的東西、用一樣的值域作答，否則不一致可能來自
    介面差異而不是判斷差異。"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "rc.render_screen(" in source
    assert "rc.ask(" in source
    assert "rc.SCREEN_FIELDS" in source
    assert "rc.SCREEN_COLUMNS" in source
    # 不得自己定義一份值域或顯示函式
    assert "SCREEN_FIELDS = {" not in source
    assert "def render_screen(" not in source


def test_輸出欄位與第一次相同():
    carrier = rs._load_carrier()
    assert carrier.SCREEN_COLUMNS == ("group_id", "personal_data", "note",
                                      "elapsed_sec", "screened_at")
