"""`src/classify_lite/baseline.py` 的測試。

每一個測試對應模組 docstring 裡的一條陳述。那份 docstring 講的是這條規則
**能宣稱什麼、不能宣稱什麼**，而那些話會隨著有人「順手改進一下」而悄悄
變成假的——基準線一旦被優化過，它就不再是基準線了。

全部是純函式測試，不讀任何資料檔：`predict()` 收的是 `clustered` 與
`length` 兩個標量，本來就不需要真實資料。凍結值的自洽性（三桶相加等於
17,365 / 58,100）在這裡驗；「重算後仍相符」要真實資料，那由使用端負責。

跑法：pytest tests/test_baseline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classify_lite import baseline as b  # noqa: E402


# --- 規則本身 ---------------------------------------------------------------
def test_歸群一律判非_human_chat_不管多短():
    # 歸群代表有固定外框。外框短不代表它是人打的——A008 的內容中位數
    # 只有 78 字元，但 160 個相異內容共享同一個 18 字元的開頭。
    assert b.predict(True, 20) == b.PRED_NOT_HUMAN
    assert b.predict(True, 78) == b.PRED_NOT_HUMAN
    assert b.predict(True, 7_634_858) == b.PRED_NOT_HUMAN


def test_散筆且短判_human_chat():
    assert b.predict(False, 20) == b.PRED_HUMAN
    assert b.predict(False, 104) == b.PRED_HUMAN       # 散筆的長度中位數


def test_散筆且長判棄權而不是猜():
    # 棄權是明寫的一類。硬塞進任何一邊都會讓準確率變成人為的。
    assert b.predict(False, 1_347) == b.PRED_ABSTAIN   # 棄權桶的長度中位數
    assert b.predict(False, 1_448_463) == b.PRED_ABSTAIN


def test_門檻邊界是嚴格小於():
    assert b.predict(False, b.HUMAN_CHAT_MAX_LEN - 1) == b.PRED_HUMAN
    assert b.predict(False, b.HUMAN_CHAT_MAX_LEN) == b.PRED_ABSTAIN


def test_門檻就是已凍結的長度切點_Q1():
    """門檻不得是本模組自己調出來的。

    掃過的候選裡 Youden J 最大的是 300，但選 251——因為它是分層早就在用
    的切點，選它才不會引入新的自由參數。有人把它改成 300（或任何「更好」
    的值）時，這條要吵出來：那一改就把基準線變成另一個被優化過的模型。
    """
    assert b.HUMAN_CHAT_MAX_LEN == 251


def test_預測值域只有三個而且不含四軸的實值():
    assert set(b.PREDICTIONS) == {"not_human_chat", "human_chat", "abstain"}
    # 中繼資料分不出 user_script／tool_generated／pipeline_relay。
    # 出現這些值代表有人把基準線改成假的四分類。
    for fake in ("user_script", "tool_generated", "pipeline_relay"):
        assert fake not in b.PREDICTIONS


def test_長度缺值要炸而不是當成_0():
    # 長度缺值與長度為 0 是兩件事。當成 0 會讓缺值的那些被靜默判成 human_chat。
    with pytest.raises(ValueError):
        b.predict(False, None)


def test_負長度要炸():
    with pytest.raises(ValueError):
        b.predict(False, -1)


def test_長度為_0_是合法輸入():
    assert b.predict(False, 0) == b.PRED_HUMAN


# --- 逐列 -------------------------------------------------------------------
def test_predict_rows_接受兩種長度欄名():
    rows = [{"clustered": True, "length": 500},
            {"clustered": False, "prompt_text_len": 10}]
    assert b.predict_rows(rows) == [b.PRED_NOT_HUMAN, b.PRED_HUMAN]


def test_predict_rows_與逐筆_predict_一致():
    rows = [{"clustered": c, "length": n}
            for c in (True, False) for n in (0, 250, 251, 10_000)]
    assert b.predict_rows(rows) == [b.predict(r["clustered"], r["length"])
                                    for r in rows]


# --- 收斂 -------------------------------------------------------------------
def test_summarise_三個桶一定都在即使是零():
    # 缺鍵與零是兩件事，而下游的比對是逐鍵做的。
    got = b.summarise([b.PRED_HUMAN], [3])
    assert set(got) == set(b.PREDICTIONS)
    assert got[b.PRED_ABSTAIN] == {"contents": 0, "requests": 0}
    assert got[b.PRED_HUMAN] == {"contents": 1, "requests": 3}


def test_summarise_沒給請求數時請求數為零():
    got = b.summarise([b.PRED_HUMAN, b.PRED_HUMAN])
    assert got[b.PRED_HUMAN] == {"contents": 2, "requests": 0}


def test_summarise_擋下未知的預測值():
    with pytest.raises(ValueError):
        b.summarise(["human"], [1])


# --- 凍結值 -----------------------------------------------------------------
def test_凍結分布三桶相加等於母體():
    """自洽性。有人改了其中一格而沒改總數時，這條會吵出來。"""
    assert (sum(v["contents"] for v in b.FROZEN_PREDICTION.values())
            == b.POPULATION_CONTENTS == 17_365)
    assert (sum(v["requests"] for v in b.FROZEN_PREDICTION.values())
            == b.POPULATION_REQUESTS == 58_100)


def test_凍結分布的鍵就是值域():
    assert set(b.FROZEN_PREDICTION) == set(b.PREDICTIONS)


def test_母體指紋是那條凍結座標():
    # 換一批資料要重新量分布，而重新量出來的是另一條基準線，不能拿去跟
    # 舊的分類器比。指紋是「還是同一批」的唯一憑據。
    assert b.POPULATION_FINGERPRINT == (
        "63af7d40e051d069dece9f14771eab6be7cb487f25c1937f6ff56ec12eb6919b")
    assert len(b.POPULATION_FINGERPRINT) == 64


def test_verify_frozen_對得上時回空清單():
    assert b.verify_frozen(b.FROZEN_PREDICTION) == []


def test_verify_frozen_一次回報全部差異而不是只回第一個():
    """分布飄了通常不只飄一格，只報第一個會讓人以為問題比實際小。"""
    bad = {
        b.PRED_NOT_HUMAN: {"contents": 11_000, "requests": 32_429},
        b.PRED_HUMAN: {"contents": 3_682, "requests": 18_000},
        b.PRED_ABSTAIN: {"contents": 2_026, "requests": 6_697},
    }
    problems = b.verify_frozen(bad)
    assert len(problems) >= 3          # 兩格差異 + 兩個總數對不上
    joined = " ".join(problems)
    assert "not_human_chat.contents" in joined
    assert "human_chat.requests" in joined


def test_verify_frozen_缺一整桶也要報():
    bad = {k: v for k, v in b.FROZEN_PREDICTION.items() if k != b.PRED_ABSTAIN}
    problems = b.verify_frozen(bad)
    assert any("abstain" in p and "缺" in p for p in problems)


def test_verify_frozen_總數對不上時要報():
    bad = {k: dict(v) for k, v in b.FROZEN_PREDICTION.items()}
    bad[b.PRED_ABSTAIN]["contents"] += 1
    problems = b.verify_frozen(bad)
    assert any("相異內容總數" in p for p in problems)


# --- 它不得被接到產品線上 ---------------------------------------------------
def test_基準線不被任何管線模組引用():
    """本模組唯一的用途是被比較。

    它一旦被 metrics／render 引用，就會有數字經由它流進 docs/，而那些
    數字看起來會像分類結果——但它從頭到尾沒看過一個字。
    """
    root = Path(__file__).resolve().parent.parent / "src"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "baseline.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "baseline" in text and "classify_lite" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"這些模組引用了基準線：{offenders}"


def test_模組不在_import_時讀任何檔案():
    # 純函式模組。會讀檔就代表有人把資料載入搬進來了，而那會讓
    # 「在沒有 680 MB 原始資料的環境裡也測得動」這件事失效。
    source = (Path(__file__).resolve().parent.parent
              / "src" / "classify_lite" / "baseline.py").read_text(encoding="utf-8")
    for forbidden in ("read_parquet", "read_csv", "open(", "load_dataset"):
        assert forbidden not in source, f"baseline.py 出現 {forbidden}"
