"""`_rescued_scratchpad/review_clusters.py` 的測試。

那支載具**由專案主人在終端執行，AI 不得執行**——它會把 `prompt_text` 印到
螢幕上，而任何經過 AI 的 tool output 的東西都會寫進 session transcript。
所以它的正確性只能這樣驗：**全部用假資料**，一筆真實內容都不碰。

本檔的 fixture 字串全是這裡現造的（"FRAME-AAA…" 之類），不是從資料來的。

載具在 repo 外（換機器時要一起帶），所以找不到它就 skip 而不是 fail——
測試套件在只有 repo 的環境裡也要跑得完。

跑法：pytest tests/test_review_clusters.py
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CARRIER = REPO.parent / "_rescued_scratchpad" / "review_clusters.py"


def _load():
    if not CARRIER.exists():
        pytest.skip(f"載具不在本機（{CARRIER}），跳過")
    spec = importlib.util.spec_from_file_location("review_clusters", CARRIER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["review_clusters"] = module
    spec.loader.exec_module(module)      # 只跑模組層：常數與 def，無 I/O
    return module


rc = _load()


# --- 代表選取 ---------------------------------------------------------------
# 「取內容長度最接近中位數的那一筆」。頭尾兩端容易是異常值，中位數那筆
# 最能代表這個群的常態；而且 build_clusters.py 與載具必須挑到同一筆，
# 否則 clusters.parquet 的 代表_sha256 與螢幕上顯示的不是同一個東西。
def test_代表取中位數那筆_奇數():
    lengths = [10, 100, 1000]
    shas = ["a", "b", "c"]
    assert rc.pick_representative(lengths, shas) == 1


def test_代表取中位數那筆_順序不影響結果():
    # 中位數 = 100，不論列的順序，挑到的都該是長度 100 的那筆。
    assert rc.pick_representative([1000, 10, 100], ["c", "a", "b"]) == 2


def test_代表偶數時平手取較短的():
    # 四筆 [10, 90, 110, 1000] 的中位數是 100，90 與 110 距離都是 10。
    # 規則是取較短的那筆——任意挑一個會讓重跑挑到不同的代表。
    idx = rc.pick_representative([10, 90, 110, 1000], ["a", "b", "c", "d"])
    assert idx == 1


def test_代表長度也平手時取_sha256_字典序小的():
    idx = rc.pick_representative([50, 50, 50], ["ccc", "aaa", "bbb"])
    assert idx == 1


def test_空群集沒有代表():
    with pytest.raises(ValueError):
        rc.pick_representative([], [])


def test_代表選取與_build_clusters_一致():
    """兩支程式的同名函式必須給同一個答案。

    這條看起來多餘，但它擋的是「有人只改了其中一支」——而改壞之後
    不會報錯，只會讓審閱紀錄講的群與代表悄悄錯開。
    """
    cases = [
        ([10, 100, 1000], ["a", "b", "c"]),
        ([10, 90, 110, 1000], ["a", "b", "c", "d"]),
        ([50, 50, 50], ["ccc", "aaa", "bbb"]),
        ([7], ["z"]),
    ]
    for lengths, shas in cases:
        med = pd.Series(lengths).median()
        expected = min(range(len(lengths)),
                       key=lambda i: (abs(lengths[i] - med), lengths[i], shas[i]))
        assert rc.pick_representative(lengths, shas) == expected


# --- 值域檢查 ---------------------------------------------------------------
# 打錯字要擋下來。不擋的話 csv 裡會出現 "remvoe"，而彙總時那一列會被
# 靜默歸到別處或消失——這種錯誤沒有測試抓得到，只能在輸入端擋。
# **值域從 rc.FIELDS 取，不在測試裡抄一份。** 抄一份的話，值域改了要改兩處，
# 而漏改的那一處會讓測試繼續驗一組已經不存在的值——看起來全綠。
@pytest.mark.parametrize("field,value", [
    (f, v) for f, vals in rc.FIELDS.items() for v in vals])
def test_值域內的值照收(field, value):
    assert rc.validate_value(field, value) == value


@pytest.mark.parametrize("field,bad", [
    ("frame_owner", "remvoe"), ("frame_owner", "TOOLS"),
    ("disposition", "delete"), ("disposition", "kept"),
    ("axis_level", "all_tree"), ("axis_level", "three"),
])
def test_打錯字被擋下(field, bad):
    with pytest.raises(ValueError):
        rc.validate_value(field, bad)


def test_空白不可當作答案():
    # 空值會變成「跳過」的偽裝，而跳過與「我判斷是 unsure」是兩件事，
    # 事後從 csv 分不出來。所以不確定必須明寫 unsure。
    for field in rc.FIELDS:
        with pytest.raises(ValueError):
            rc.validate_value(field, "")
        with pytest.raises(ValueError):
            rc.validate_value(field, "   ")


def test_唯一前綴可以接受():
    assert rc.validate_value("frame_owner", "t") == "tool"
    assert rc.validate_value("disposition", "batch") == "batch_project"
    assert rc.validate_value("axis_level", "inv") == "invocation_only"


def test_有歧義的前綴被擋下():
    # frame_owner 的 "u" 同時對到 user 與 unsure。這種時候必須擋下來，
    # 不能挑一個——挑錯的那次不會報錯，只會安靜地記下錯的判斷。
    with pytest.raises(ValueError):
        rc.validate_value("frame_owner", "u")
    # 但 "n" 只對到 none，不歧義，照收（順帶驗大小寫無關）。
    assert rc.validate_value("frame_owner", "N") == "none"


def test_大小寫不敏感():
    assert rc.validate_value("frame_owner", "TOOL") == "tool"
    assert rc.validate_value("disposition", "Batch_Project") == "batch_project"


def test_未知欄位被擋下():
    with pytest.raises(ValueError):
        rc.validate_value("not_a_field", "tool")


# --- 輸出欄位不含明文 -------------------------------------------------------
def test_輸出欄位清單本身不含明文欄名():
    rc.check_output_columns(rc.REVIEW_COLUMNS)          # 不該 raise


@pytest.mark.parametrize("bad", ["prompt_text", "text", "content",
                                 "message", "prefix", "excerpt", "PROMPT_TEXT"])
def test_明文欄名硬擋(bad):
    with pytest.raises(AssertionError):
        rc.check_output_columns(list(rc.REVIEW_COLUMNS) + [bad])


def test_寫出的_csv_只有七個欄位而且都不是內容(tmp_path):
    path = tmp_path / "cluster_review.csv"
    rc.append_review(path, dict(group_id="A001", frame_owner="tool",
                                disposition="remove", axis_level="all_three",
                                note="", elapsed_sec=12.3,
                                reviewed_at="2026-09-06T18:00:00"))
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == list(rc.REVIEW_COLUMNS)
    rc.check_output_columns(rows[0].keys())
    assert rows[0]["group_id"] == "A001"
    assert rows[0]["note"] == ""        # 程式不預填 note


def test_多寫的欄位不會被帶進檔案(tmp_path):
    # 就算呼叫端不小心塞了額外的鍵，寫出去的仍只有 REVIEW_COLUMNS。
    path = tmp_path / "r.csv"
    rc.append_review(path, dict(group_id="A001", frame_owner="tool",
                                disposition="keep", axis_level="none",
                                note="n", elapsed_sec=1.0,
                                reviewed_at="t", 額外欄="x"))
    with path.open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.DictReader(fh).fieldnames) == list(rc.REVIEW_COLUMNS)


def test_缺欄位會被擋下(tmp_path):
    with pytest.raises(ValueError):
        rc.append_review(tmp_path / "r.csv", dict(group_id="A001"))


# --- 中斷續跑 ---------------------------------------------------------------
def _clusters(ids_sizes, ratios=None):
    """假的 clusters 表。ratios 不給就照順序遞減，讓規模順序與比例順序一致。"""
    rows = []
    for i, (g, n) in enumerate(ids_sizes):
        ratio = ratios[i] if ratios is not None else 0.9 - i * 0.1
        rows.append({"group_id": g, "相異內容數": n, "請求數": n * 2,
                     "前綴佔中位內容長度比例": ratio})
    return pd.DataFrame(rows)


def test_沒有紀錄檔時全部都要審(tmp_path):
    assert rc.load_done(tmp_path / "nope.csv") == set()


def test_續跑會跳過已審的而且不跳過未審的(tmp_path):
    path = tmp_path / "r.csv"
    for gid in ("A001", "A003"):
        rc.append_review(path, dict(group_id=gid, frame_owner="tool",
                                    disposition="remove", axis_level="none",
                                    note="", elapsed_sec=1.0, reviewed_at="t"))
    done = rc.load_done(path)
    assert done == {"A001", "A003"}
    clusters = _clusters([("A001", 100), ("A002", 50), ("A003", 30), ("A004", 10)])
    todo = rc.pending_groups(clusters, done)
    assert list(todo["group_id"]) == ["A002", "A004"]


def test_續跑不會重複寫入同一群(tmp_path):
    path = tmp_path / "r.csv"
    rc.append_review(path, dict(group_id="A001", frame_owner="tool",
                                disposition="remove", axis_level="none",
                                note="", elapsed_sec=1.0, reviewed_at="t"))
    clusters = _clusters([("A001", 100), ("A002", 50)])
    todo = rc.pending_groups(clusters, rc.load_done(path))
    assert "A001" not in set(todo["group_id"])
    # 第二輪只寫 A002，csv 應該剛好兩列、group_id 不重複
    rc.append_review(path, dict(group_id="A002", frame_owner="user",
                                disposition="keep", axis_level="all_three",
                                note="", elapsed_sec=2.0, reviewed_at="t"))
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    gids = [r["group_id"] for r in rows]
    assert gids == ["A001", "A002"]
    assert len(gids) == len(set(gids))


# --- 審閱順序：先易後難 -----------------------------------------------------
# 不是按規模排序。規模大的群不一定好判——A001 有 974 個相異內容但前綴只佔
# 內容的一成，正是最難判的那一類。用規模排序會把最難的排在最前面，而第一批
# 的判斷同時也在定義後面所有判斷的標準。
def test_A001_永遠第一_即使它最小而且比例最低():
    clusters = _clusters([("A002", 900), ("B001", 300), ("A001", 10)],
                         ratios=[0.95, 0.80, 0.01])
    todo = rc.pending_groups(clusters, set())
    assert list(todo["group_id"])[0] == "A001"


def test_A001_已審過時不會憑空出現():
    clusters = _clusters([("A002", 900), ("B001", 300), ("A001", 10)],
                         ratios=[0.95, 0.80, 0.01])
    todo = rc.pending_groups(clusters, {"A001"})
    assert "A001" not in set(todo["group_id"])
    assert list(todo["group_id"]) == ["A002", "B001"]


def test_A001_之後按前綴佔比由高到低():
    clusters = _clusters([("A002", 10), ("A003", 20), ("B001", 30)],
                         ratios=[0.10, 0.90, 0.50])
    todo = rc.pending_groups(clusters, set())
    assert list(todo["group_id"]) == ["A003", "B001", "A002"]


def test_比例相同時按相異內容數由大到小():
    clusters = _clusters([("A002", 10), ("A003", 900), ("B001", 300)],
                         ratios=[0.5, 0.5, 0.5])
    todo = rc.pending_groups(clusters, set())
    assert list(todo["group_id"]) == ["A003", "B001", "A002"]


def test_規模最大的不一定排前面():
    # 這條擋的正是「改回按規模排序」。900 筆但比例 0.01 的那群要排最後。
    clusters = _clusters([("A002", 900), ("A003", 20), ("B001", 30)],
                         ratios=[0.01, 0.90, 0.50])
    todo = rc.pending_groups(clusters, set())
    assert list(todo["group_id"]) == ["A003", "B001", "A002"]


def test_比例是_NaN_的群排最後():
    clusters = _clusters([("A002", 10), ("A003", 20)], ratios=[float("nan"), 0.1])
    todo = rc.pending_groups(clusters, set())
    assert list(todo["group_id"]) == ["A003", "A002"]


def test_排序不會把輔助欄留在輸出裡():
    clusters = _clusters([("A002", 10), ("A003", 20)])
    todo = rc.pending_groups(clusters, set())
    assert "_first" not in todo.columns
    assert set(todo.columns) == set(clusters.columns)


def test_紀錄檔壞掉時當作沒審過而不是炸掉(tmp_path):
    path = tmp_path / "r.csv"
    path.write_bytes(b"\xff\xfe not a csv \x00\x01")
    assert isinstance(rc.load_done(path), set)      # 不 raise


# --- 對照樣本的抽取 ---------------------------------------------------------
def test_對照樣本不含代表本身():
    shas = [f"sha{i:03d}" for i in range(10)]
    got = rc.sample_others(shas, "sha003", "A001")
    assert "sha003" not in got
    assert len(got) == rc.N_OTHERS


def test_對照樣本是決定性的():
    # 中斷續跑要看到同一批對照，否則審閱紀錄講的不是同一個東西。
    shas = [f"sha{i:03d}" for i in range(50)]
    a = rc.sample_others(shas, "sha000", "A007")
    b = rc.sample_others(shas, "sha000", "A007")
    assert a == b


def test_不同群抽到的對照不同():
    shas = [f"sha{i:03d}" for i in range(50)]
    assert rc.sample_others(shas, "sha000", "A001") != \
           rc.sample_others(shas, "sha000", "A002")


def test_群只有一筆時沒有對照():
    assert rc.sample_others(["only"], "only", "A001") == []


def test_群只有兩筆時只有一個對照():
    got = rc.sample_others(["a", "b"], "a", "A001")
    assert got == ["b"]


# --- 讀檔失敗不中斷 ---------------------------------------------------------
def test_讀不到的檔回_None_而不是_raise(tmp_path):
    assert rc.read_prompt_text(tmp_path, "不存在/的/路徑.json") is None


def test_不是_json_的檔回_None(tmp_path):
    (tmp_path / "bad.json").write_text("{ 這不是 json", encoding="utf-8")
    assert rc.read_prompt_text(tmp_path, "bad.json") is None


def test_沒有_prompt_text_欄位的_json_回_None(tmp_path):
    (tmp_path / "ok.json").write_text('{"request": {}}', encoding="utf-8")
    assert rc.read_prompt_text(tmp_path, "ok.json") is None


def test_讀得到時回傳字串(tmp_path):
    # fixture 內容是這裡現造的，不是真實資料。
    (tmp_path / "ok.json").write_text(
        '{"request": {"prompt_text": "FRAME-AAA payload-001"}}', encoding="utf-8")
    assert rc.read_prompt_text(tmp_path, "ok.json") == "FRAME-AAA payload-001"


# --- 不預判 -----------------------------------------------------------------
def test_三個軸都沒有預設值():
    """值域裡不得有「預設」的概念——預設值會變成錨點。

    這條驗的是資料結構：FIELDS 只有值域，沒有 default 欄位；
    FIELD_HELP 的每個值都有對應說明（或空字串），沒有哪個被標成推薦。
    """
    assert not hasattr(rc, "DEFAULTS")
    for field, allowed in rc.FIELDS.items():
        assert set(rc.FIELD_HELP[field]) == set(allowed)
        for value in allowed:
            help_text = rc.FIELD_HELP[field][value]
            for word in ("建議", "推薦", "預設", "多半", "通常"):
                assert word not in help_text


# --- 前綴篩檢模式（--screen）------------------------------------------------
# 篩檢是送 LLM 之前的閘門。它只問一題，因為多問一題會讓人一邊判風險一邊
# 判內容，而風險判斷需要保守、內容判斷需要理解，兩者的心態相反。
def test_篩檢只有一題():
    assert list(rc.SCREEN_FIELDS) == ["personal_data"]


def test_篩檢的值域是三個():
    assert rc.SCREEN_FIELDS["personal_data"] == ("clean", "flagged", "unsure")


def test_篩檢欄位有五欄且都不是內容():
    assert rc.SCREEN_COLUMNS == ("group_id", "personal_data", "note",
                                 "elapsed_sec", "screened_at")
    rc.check_output_columns(rc.SCREEN_COLUMNS)


@pytest.mark.parametrize("value", ["clean", "flagged", "unsure"])
def test_篩檢值域內的值照收(value):
    assert rc.validate_value("personal_data", value, rc.SCREEN_FIELDS) == value


@pytest.mark.parametrize("bad", ["ok", "safe", "clen", "yes"])
def test_篩檢打錯字被擋下(bad):
    with pytest.raises(ValueError):
        rc.validate_value("personal_data", bad, rc.SCREEN_FIELDS)


def test_篩檢空白不可當作答案():
    with pytest.raises(ValueError):
        rc.validate_value("personal_data", "", rc.SCREEN_FIELDS)


def test_篩檢的唯一前綴可以接受():
    assert rc.validate_value("personal_data", "c", rc.SCREEN_FIELDS) == "clean"
    assert rc.validate_value("personal_data", "f", rc.SCREEN_FIELDS) == "flagged"


def test_篩檢的三軸值域不會誤用到篩檢欄位():
    # 用預設的 FIELDS 問 personal_data 應該是未知欄位。
    with pytest.raises(ValueError):
        rc.validate_value("personal_data", "clean")


def test_篩檢沒有預設值():
    assert set(rc.SCREEN_FIELD_HELP["personal_data"]) == \
        set(rc.SCREEN_FIELDS["personal_data"])
    for value, help_text in rc.SCREEN_FIELD_HELP["personal_data"].items():
        for word in ("建議", "推薦", "預設", "多半", "通常"):
            assert word not in help_text


def test_篩檢寫出的_csv_只有五欄(tmp_path):
    path = tmp_path / "prefix_screen.csv"
    rc.append_review(path, dict(group_id="A001", personal_data="clean",
                                note="", elapsed_sec=5.0,
                                screened_at="2026-09-06T20:00:00"),
                     columns=rc.SCREEN_COLUMNS)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == list(rc.SCREEN_COLUMNS)
    assert rows[0]["personal_data"] == "clean"
    assert rows[0]["note"] == ""          # 程式不預填


def test_篩檢的多餘欄位不會落地(tmp_path):
    path = tmp_path / "s.csv"
    rc.append_review(path, dict(group_id="A001", personal_data="flagged",
                                note="n", elapsed_sec=1.0, screened_at="t",
                                prefix_raw="不該落地的東西"),
                     columns=rc.SCREEN_COLUMNS)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        text = path.read_text(encoding="utf-8-sig")
    assert "prefix_raw" not in text
    assert "不該落地的東西" not in text


def test_篩檢缺欄位會被擋下(tmp_path):
    with pytest.raises(ValueError):
        rc.append_review(tmp_path / "s.csv", dict(group_id="A001"),
                         columns=rc.SCREEN_COLUMNS)


def test_篩檢與審閱用同一個續跑機制(tmp_path):
    path = tmp_path / "s.csv"
    rc.append_review(path, dict(group_id="A001", personal_data="clean",
                                note="", elapsed_sec=1.0, screened_at="t"),
                     columns=rc.SCREEN_COLUMNS)
    clusters = _clusters([("A001", 100), ("A002", 50)])
    todo = rc.pending_groups(clusters, rc.load_done(path))
    assert list(todo["group_id"]) == ["A002"]


def test_篩檢與審閱共用排序():
    # 兩個模式看到的順序必須一致，否則「第幾批」在兩份紀錄裡不是同一批。
    clusters = _clusters([("A002", 900), ("B001", 300), ("A001", 10)],
                         ratios=[0.95, 0.80, 0.01])
    assert list(rc.pending_groups(clusters, set())["group_id"])[0] == "A001"


def test_篩檢模式的路徑與審閱分開():
    assert rc.SCREEN_CSV != rc.REVIEW_CSV
    assert rc.SCREEN_CSV.name == "prefix_screen.csv"


def test_screen_旗標存在且預設為關():
    import argparse
    parser = argparse.ArgumentParser()
    del parser
    # 直接驗 main 的介面：--screen 存在，且不帶參數時走完整審閱。
    source = (REPO.parent / "_rescued_scratchpad" / "review_clusters.py"
              ).read_text(encoding="utf-8")
    assert '"--screen"' in source
    assert "if args.blind_label:" in source
    assert "return run_screen() if args.screen else run_review()" in source


# --- 盲性 -------------------------------------------------------------------
def test_載具不讀也不顯示_LLM_判定結果():
    """載具讀 clusters.parquet 而非 judge_output.csv，這是設計不是巧合。

    一旦人看過 LLM 的答案，該群的人工判定就不再是獨立參照，一致率也就
    失去意義——它會變成「人有多容易被說服」的量測，而那個數字看起來與
    真正的一致率完全一樣，事後也分不出來。
    規則寫在 ref/annotation_protocol.md 第二節〈盲性〉。
    """
    source = CARRIER.read_text(encoding="utf-8")
    code = source.split("from __future__ import annotations", 1)[1]
    for token in ("judge_output", "judge_clusters", "confidence"):
        assert token not in code, f"載具碰到了 LLM 判定相關的 {token}"


def test_載具只讀這七個檔():
    source = CARRIER.read_text(encoding="utf-8")
    paths = [l.split("=")[0].strip() for l in source.splitlines()
             if "HERE /" in l and "=" in l]
    assert set(paths) == {"CLUSTERS", "MEMBERS", "PREFIXES",
                          "REVIEW_CSV", "SCREEN_CSV", "BLIND_SAMPLE_CSV",
                          "BLIND_LABEL_CSV", "BLIND_LABEL_V2_CSV"}


# --- 人工盲標模式 -----------------------------------------------------------
def test_盲標顯示順序用獨立種子且不沿用樣本列序():
    given = [f"X{i:03d}" for i in range(20)]
    first = rc.blind_order(given)
    second = rc.blind_order(given)
    assert rc.BLIND_ORDER_SEED != 20260911  # 抽樣腳本的 SEED
    assert first == second
    assert first != given
    assert first != sorted(given)
    assert set(first) == set(given)


def test_實際盲標顯示順序與樣本檔列序不同():
    if not rc.BLIND_SAMPLE_CSV.exists():
        pytest.skip("盲標樣本不在本機")
    file_order = rc.load_blind_sample()
    assert rc.blind_order(file_order) != file_order


def test_盲標樣本只讀_group_id_不帶入分層(tmp_path):
    path = tmp_path / "sample.csv"
    pd.DataFrame({
        "group_id": ["X001", "X002"],
        "stratum": ["DO-NOT-SHOW-1", "DO-NOT-SHOW-2"],
        "reason": ["MODEL-ONLY-1", "MODEL-ONLY-2"],
    }).to_csv(path, index=False, encoding="utf-8-sig")
    assert rc.load_blind_sample(path) == ["X001", "X002"]


def test_盲標每次最多三批每批十群():
    order = [f"X{i:03d}" for i in range(40)]
    items = rc.blind_run_items(order, {"X000", "X001"})
    assert len(items) == rc.BLIND_BATCH_SIZE * rc.BLIND_MAX_BATCHES == 30
    assert items[0] == (3, "X002")
    assert items[-1] == (32, "X031")


def test_must_axis_constraints_自動產生():
    assert rc.must_axis_levels() == {
        "tool_injected": "none",
        "service_relay": "not_attributable",
        "insufficient": "insufficient_data",
    }
    assert rc.must_axis_level("batch_project") is None
    custom = (("custom", "none", "must", "fixture"),)
    assert rc.must_axis_level("custom", custom) == "none"


def test_盲標紀錄欄位與續跑契約(tmp_path):
    path = tmp_path / "blind_labels.csv"
    rc.append_review(path, {
        "group_id": "X001",
        "frame_owner": "unsure",
        "disposition": "unsure",
        "axis_level": "unsure",
        "note": "",
        "elapsed_sec": 1.2,
        "labeled_at": "2026-09-11T12:00:00",
        "seed_order": 7,
    }, columns=rc.BLIND_LABEL_COLUMNS)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == rc.BLIND_LABEL_COLUMNS
    assert rows[0]["note"] == ""
    assert rc.load_blind_done(path) == {"X001"}


def test_盲標_stdout_不洩漏分層或模型欄位且順序另行打散(
        tmp_path, monkeypatch, capsys):
    ids = [f"X{i:03d}" for i in range(12)]
    sample_path = tmp_path / "blind_label_sample.csv"
    label_path = tmp_path / "blind_labels.csv"
    v2_label_path = tmp_path / "blind_labels.v2.csv"
    cluster_path = tmp_path / "clusters.parquet"
    member_path = tmp_path / "members.parquet"
    cluster_path.write_bytes(b"fixture")
    member_path.write_bytes(b"fixture")
    pd.DataFrame({
        "group_id": ids,
        "stratum": ["HIDDEN-ALPHA" if i % 2 else "HIDDEN-BETA"
                    for i in range(len(ids))],
        "reason": ["MODEL-REASON-SENTINEL"] * len(ids),
        "confidence": ["MODEL-CONFIDENCE-SENTINEL"] * len(ids),
        "prompt_sha256": ["MODEL-FINGERPRINT-SENTINEL"] * len(ids),
    }).to_csv(sample_path, index=False, encoding="utf-8-sig")

    clusters = pd.DataFrame({"group_id": ids})
    members = pd.DataFrame({"group_id": ids})
    monkeypatch.setattr(rc, "BLIND_SAMPLE_CSV", sample_path)
    monkeypatch.setattr(rc, "BLIND_LABEL_CSV", label_path)
    monkeypatch.setattr(rc, "BLIND_LABEL_V2_CSV", v2_label_path)
    monkeypatch.setattr(rc, "CLUSTERS", cluster_path)
    monkeypatch.setattr(rc, "MEMBERS", member_path)
    monkeypatch.setattr(
        rc.pd, "read_parquet",
        lambda path: clusters.copy() if path == cluster_path else members.copy())

    monkeypatch.syspath_prepend(str(REPO))
    from src import config
    monkeypatch.setattr(config, "lite_raw_dir", lambda: tmp_path)
    seen = []
    monkeypatch.setattr(
        rc, "render_group",
        lambda row, member_rows, raw_root: seen.append(row["group_id"]) or 0)
    monkeypatch.setattr(rc, "ask", lambda field: rc.FIELDS[field][0])
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert rc.run_blind_label() == 0
    stdout = capsys.readouterr().out
    for leaked in (
        "stratum", "HIDDEN-ALPHA", "HIDDEN-BETA", "reason",
        "MODEL-REASON-SENTINEL", "confidence",
        "MODEL-CONFIDENCE-SENTINEL", "prompt_sha256",
        "MODEL-FINGERPRINT-SENTINEL", "seed_order",
    ):
        assert leaked not in stdout
    assert seen != ids
    assert seen != sorted(ids)
    assert set(seen) == set(ids)

    with v2_label_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == rc.BLIND_LABEL_COLUMNS
    assert {row["axis_level"] for row in rows} == {"none"}
    assert "此值由 must 約束決定" in stdout
    expected_positions = {gid: i for i, gid in enumerate(rc.blind_order(ids), 1)}
    assert {row["group_id"]: int(row["seed_order"]) for row in rows} == \
        expected_positions


# --- 檢視模式（--inspect）：只顯示，不問不寫 --------------------------------
#
# 這個模式存在的理由是**紀錄檔的純度**：看過 LLM 判定的那幾群，其人工判定
# 已經不是獨立參照；若用審閱模式看，那幾列會混進 cluster_review.csv 而事後
# 分不出來。所以它的核心性質只有一條——**不寫任何檔案**——而那條要用測試釘住，
# 因為它是「沒有做某件事」，沒有任何輸出可以顯示它成立。

@pytest.mark.parametrize("spec,expect", [
    ("A008,A010,A018", ["A008", "A010", "A018"]),
    (" a008 , a010 ", ["A008", "A010"]),          # 去空白、轉大寫
    ("A008,A008,A010", ["A008", "A010"]),         # 去重
    ("A018,A008", ["A018", "A008"]),              # **順序照給的來，不重排**
])
def test_inspect_的群清單解析(spec, expect):
    assert rc.parse_inspect(spec) == expect


@pytest.mark.parametrize("spec", ["", "   ", ",,,"])
def test_inspect_空清單被擋下(spec):
    with pytest.raises(SystemExit):
        rc.parse_inspect(spec)


def test_inspect_不重排成先易後難():
    """`pending_groups()` 的排序是為了建立常態感，那是審閱的需求。

    檢視的需求相反：回答同一個問題的群要連著看。兩個排序都對，但套錯了
    不會有徵兆——螢幕上只是換了個順序。
    """
    given = "A017,A019,A008"
    assert rc.parse_inspect(given) == ["A017", "A019", "A008"]


def test_inspect_沒有任何寫檔路徑():
    """**這是這個模式唯一真正的性質。**

    用原始碼比對而不是跑一遍：跑一遍要有明文才跑得動，而測試不碰明文。
    """
    import ast
    source = CARRIER.read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(source).body
              if isinstance(n, ast.FunctionDef) and n.name == "run_inspect")
    # **把 docstring 拿掉再比。** docstring 裡正寫著「不呼叫 append_review()、
    # 不碰 REVIEW_CSV」——說明自己會讓比對命中，第一版就是這樣紅的。
    stmts = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                            and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = "\n".join(ast.unparse(s) for s in stmts)
    for token in ("append_review", "REVIEW_CSV.open", "open(", "to_csv",
                  "write_text", "SCREEN_CSV"):
        assert token not in code, f"run_inspect 碰到了寫檔相關的 {token}"
    # REVIEW_CSV 只准出現在提示文字裡（.name），不准被開啟或寫入
    assert "REVIEW_CSV.name" in code


def test_審閱模式沒有挑群的參數():
    """**刻意缺席的東西也要釘。**

    一個能挑群的記錄模式，就是 --inspect 要避開的那個危險本身：
    它讓人可以只記錄自己挑的那幾群，而挑的依據無從查證。
    缺席不會有任何徵兆，所以由測試代替記憶。
    """
    source = CARRIER.read_text(encoding="utf-8")
    args = [l for l in source.splitlines() if "add_argument(" in l]
    assert len(args) == 3, args
    flags = source.split("def main(", 1)[1]
    assert '"--only"' not in flags
    assert '"--groups"' not in flags


def test_inspect_與_screen_互斥():
    """一個判風險、一個看內容，心態相反（見檔頭〈兩個模式的關係〉）。"""
    with pytest.raises(SystemExit):
        rc.main(["--screen", "--inspect", "A008"])


def test_盲標與其他模式互斥():
    with pytest.raises(SystemExit):
        rc.main(["--blind-label", "--screen"])
    with pytest.raises(SystemExit):
        rc.main(["--blind-label", "--inspect", "A008"])


def test_inspect_模式在旗標清單裡有說明():
    source = CARRIER.read_text(encoding="utf-8")
    assert "--inspect A008,A010" in source          # 檔頭的用法列
    assert "不問三軸、不寫任何檔案" in source
