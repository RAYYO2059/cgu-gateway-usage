"""前置規則的三件會靜默出錯的事。

四條規則決定分類母數，而母數錯了之後**沒有任何東西會失敗**——分類器照跑、
佔比照算，只是分母不對。所以這裡測的不是「算得對不對」（那要有資料才驗得了），
是三個結構性質：

1. 判定字串表在版控裡，而且形狀正確。它消失過一次，見 DESIGN_NOTES
   〈進入文件的數字，其產生程式必須在版控內〉。
2. 規則指派在 sha256 層且唯一，所以四項相加等於排除總量。
3. 明文不落地的斷言真的會擋下來。

全部不讀 data/，沿用 test_college 的判斷：邏輯對不對是它自己的事。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.classify_lite import markers, prefilter


# ---------------------------------------------------------------------------
# 一、判定字串表
# ---------------------------------------------------------------------------
def test_判定字串表存在且欄位齊全():
    table = markers.load_marker_table()
    assert set(table.columns) >= {"規則", "標記名", "判定字串", "是否為排除項",
                                  "加入日期", "來源"}
    assert not table["判定字串"].isna().any(), "判定字串不可留空"
    assert table["標記名"].is_unique, "標記名是旗標欄名的一部分，不可重複"


def test_規則三與四的標記數是六與二():
    """DESIGN_NOTES 的「八種」數的就是這兩組。

    數字寫死在測試裡是刻意的：這兩個數被引用在 OVERVIEW 4.2 與 DESIGN_NOTES
    兩處，表被改動時要有人重新確認那兩處，而不是靜靜地對不上。
    """
    table = markers.load_marker_table()
    rules = table[table["是否為排除項"] != "是"]["規則"].value_counts().to_dict()
    assert rules == {"3": 6, "4": 2}


def test_ambient_標記標成排除項():
    """它是明確不攔的那一條。標錯的話規則 3 會多攔一批意圖屬於使用者的請求。"""
    table = markers.load_marker_table()
    ambient = table[table["標記名"] == "ambient_ui"]
    assert len(ambient) == 1
    assert ambient["是否為排除項"].iloc[0] == "是"
    columns = markers.marker_columns(table)
    assert "exclude_ambient_ui" in columns
    assert prefilter._rule_of_column("exclude_ambient_ui") is None, (
        "排除項不可被當成規則，否則它會反過來攔掉自己聲明不攔的東西")


def test_旗標欄名帶著規則編號():
    table = markers.load_marker_table()
    columns = markers.marker_columns(table)
    assert "r3_title_gen" in columns
    assert "r4_data_block" in columns
    assert prefilter._rule_of_column("r3_title_gen") == 3
    assert prefilter._rule_of_column("r4_data_block") == 4


# ---------------------------------------------------------------------------
# 二、指派唯一性
# ---------------------------------------------------------------------------
def _frame(rows: list[dict]) -> pd.DataFrame:
    columns = ["prompt_text_sha256", "n_requests", "r1", "r2",
               "r3_title_gen", "r4_data_block", "exclude_ambient_ui"]
    flags = [c for c in columns if c not in ("prompt_text_sha256", "n_requests")]
    filled = [{**{c: False for c in flags}, **row} for row in rows]
    return pd.DataFrame(filled, columns=columns)


FLAGS = ["r3_title_gen", "r4_data_block", "exclude_ambient_ui"]


def test_同時命中多條時只算一次():
    """這是「四項相加多 3」的成因，改成 sha256 層指派之後不該再發生。"""
    frame = _frame([
        {"prompt_text_sha256": "a", "n_requests": 1, "r1": True, "r2": False,
         "r4_data_block": True},                      # 同時命中 1 與 4
        {"prompt_text_sha256": "b", "n_requests": 1, "r1": False, "r2": False,
         "r3_title_gen": True},
        {"prompt_text_sha256": "c", "n_requests": 1, "r1": False, "r2": False},
    ])
    out = prefilter.assign_rules(frame, FLAGS)

    assert out.loc[0, "rule"] == prefilter.RULE_LABELS[1], "編號小的優先"
    assert out.loc[0, "n_rules_hit"] == 2, "命中幾條仍要看得見"
    assert out.loc[2, "rule"] == prefilter.UNASSIGNED

    removed = int((out["rule"] != prefilter.UNASSIGNED).sum())
    per_rule = out[out["rule"] != prefilter.UNASSIGNED]["rule"].value_counts()
    assert per_rule.sum() == removed == 2, (
        "四項相加必須等於排除總量——這張表要能拿來驗算母數")


def test_排除項不會造成指派():
    frame = _frame([{"prompt_text_sha256": "a", "n_requests": 3,
                     "r1": False, "r2": False, "exclude_ambient_ui": True}])
    out = prefilter.assign_rules(frame, FLAGS)
    assert out.loc[0, "rule"] == prefilter.UNASSIGNED
    assert out.loc[0, "n_rules_hit"] == 0


def test_優先序是一到四():
    frame = _frame([{"prompt_text_sha256": "a", "n_requests": 1,
                     "r1": False, "r2": True,
                     "r3_title_gen": True, "r4_data_block": True}])
    out = prefilter.assign_rules(frame, FLAGS)
    assert out.loc[0, "rule"] == prefilter.RULE_LABELS[2]
    assert out.loc[0, "n_rules_hit"] == 3


# ---------------------------------------------------------------------------
# 三、明文不落地
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("column", ["prompt_text", "content", "message"])
def test_輸出含明文欄位就_raise(column):
    """明文一旦落地就會跟著 data/ 被備份、被複製，再也收不回來。"""
    frame = pd.DataFrame({"prompt_text_sha256": ["a"], column: ["某段內容"]})
    with pytest.raises(AssertionError, match="明文"):
        markers.check_columns(frame)


def test_只有雜湊與旗標時不_raise():
    frame = pd.DataFrame({"prompt_text_sha256": ["a"], "r3_title_gen": [True]})
    markers.check_columns(frame)


def test_模型判定用_startswith_不是_endpoint():
    """兩者的結果不同，而且不是小差異——見 prefilter 模組 docstring。"""
    assert prefilter.EMBEDDING_MODELS == ("text-embedding-", "bge-m3")
    assert prefilter.IMAGE_MODELS == ("gpt-image-",)


# ---------------------------------------------------------------------------
# 四、分割檢查
# ---------------------------------------------------------------------------
def _summary(pairs: list[tuple[str, int, int]]) -> pd.DataFrame:
    return pd.DataFrame(pairs, columns=["rule", "相異內容", "請求數"])


def test_互斥分割成立時不_raise():
    summary = _summary([("1 向量化", 587, 1010), (prefilter.UNASSIGNED, 3, 7)])
    prefilter.check_partition(summary, 590, 1017)


def test_總和超過母體就_raise():
    """這是「多算 3」那次的形狀：只多一點點，數字看起來完全合理。

    這個測試存在的理由是〈測試可以驗一個永遠不會被執行的路徑〉那條——
    檢查加了卻從沒被觸發過的話，沒有人知道它會不會擋。
    """
    summary = _summary([("1 向量化", 587, 1010),
                        (prefilter.UNASSIGNED, 6, 7)])   # 多 3
    with pytest.raises(AssertionError, match="互斥分割"):
        prefilter.check_partition(summary, 590, 1017)


def test_單組超過母體就_raise():
    """這是 383,678 那次的形狀：把整數欄當旗標加總，量級大到不可能。"""
    summary = _summary([("3 工具自動發出", 383_678, 383_678)])
    with pytest.raises(AssertionError, match="超過母體"):
        prefilter.check_partition(summary, 106_993, 106_993)


def test_請求數那一欄也會被檢查():
    """相異內容對得上、請求數對不上時同樣要擋——兩欄各自是一個分割。"""
    summary = _summary([("1 向量化", 587, 1010),
                        (prefilter.UNASSIGNED, 3, 99)])
    with pytest.raises(AssertionError, match="請求數"):
        prefilter.check_partition(summary, 590, 1017)
