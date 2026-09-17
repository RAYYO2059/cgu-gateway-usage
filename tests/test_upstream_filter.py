"""`docs/UPSTREAM_FILTER.md` 的數字要接回 manifest 與 L1。

這份文件記的是**上游在資料進來之前移除了多少**。它的每一個數字都來自
`data/00_raw_lite/*/*/_prepare_report.json`，而那些檔在 gitignore 裡——
所以文件是唯一留在版控裡的紀錄，也因此它一旦過期就沒有東西擋得住。

原始資料不在本機時 skip 而不是 fail，與其他碰 `data/` 的測試同一個處理。

跑法：pytest tests/test_upstream_filter.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "UPSTREAM_FILTER.md"

_CACHE: dict = {}


def _manifests():
    """每個擷取日一份 `_prepare_report.json`。"""
    if "man" not in _CACHE:
        from src import config
        root = config.lite_raw_dir()
        paths = sorted(root.glob("*/*/_prepare_report.json"))
        if not paths:
            pytest.skip("原始資料不在本機")
        _CACHE["man"] = {p.parent.parent.name: json.loads(p.read_text(encoding="utf-8"))
                         for p in paths}
    return _CACHE["man"]


def _lite():
    if "lite" not in _CACHE:
        from src.extract_lite import load_dataset
        _CACHE["lite"] = load_dataset()
    return _CACHE["lite"]


def _doc():
    return DOC.read_text(encoding="utf-8")


def _table(n_cells: int = 7):
    """以日期開頭的表格列，依欄數挑表。

    **依欄數挑，不依出現順序。** 第二節與第七節的列都以同一個日期開頭，
    只用正規式抓會把兩張表混在一起——第一版就是這樣把第七節的
    「規則 4 請求數」當成第二節的 `source_files` 來比。
    """
    rows = {}
    for line in _doc().splitlines():
        m = re.match(r"\|\s*`(\d{4}-\d{2}-\d{2})`\s*\|(.+)\|\s*$", line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) == n_cells:
            rows[m.group(1)] = cells
    assert rows, f"解析不出任何一列 {n_cells} 欄的表格"
    return rows


def _num(s):
    return int(s.replace(",", ""))


# --- 平帳 -------------------------------------------------------------------

def test_每一份_manifest_都平帳():
    """`written + excluded + skipped = source_files`。

    **逐份驗，不只驗合計。** 合計平帳而個別不平，代表有兩份的誤差互相抵銷
    ——那種情況下總數看起來完全正常。
    """
    for name, d in _manifests().items():
        assert (d["written_records"] + d["excluded_internal_prompts"]
                + d["skipped_records"]) == d["source_files"], name


def test_沒有失敗的檔():
    """第一節寫「沒有第四類去向，也沒有失敗的檔」。有了就要改文件。"""
    for name, d in _manifests().items():
        assert d["failed_files"] == 0, name
        assert d["failures"] == [], name


def test_manifest_的_schema_與請求記錄同一版():
    """第一節據此說「這不是某一天的例外格式」。"""
    got = {(d["schema"]["name"], d["schema"]["version"]) for d in _manifests().values()}
    assert got == {("ai_platform_request", "1.1-lite")}
    assert "`ai_platform_request` `1.1-lite`" in _doc()


# --- 合計 -------------------------------------------------------------------

def test_文件記的合計與_manifest_相符():
    man = _manifests()
    s = sum(d["source_files"] for d in man.values())
    w = sum(d["written_records"] for d in man.values())
    e = sum(d["excluded_internal_prompts"] for d in man.values())
    doc = _doc()
    for n in (s, w, e):
        assert f"{n:,}" in doc, f"文件裡找不到 {n:,}"
    assert f"{100 * e / s:.2f}%" in doc
    assert f"{len(man)} 份" in doc


def test_written_records_合計等於_L1_列數():
    """文件第一節的整個論證靠這一條：manifest 講的就是我們手上這批。"""
    w = sum(d["written_records"] for d in _manifests().values())
    assert w == len(_lite())


def test_每日的_written_records_與_L1_逐日相符():
    """第一節說「逐日相符，所以那張表是精確對照不是估計」。"""
    byd = _lite().groupby("date_dir").size().to_dict()
    for name, d in _manifests().items():
        assert int(byd.get(name, 0)) == d["written_records"], name


# --- 逐日表 -----------------------------------------------------------------

def test_逐日表的每一列都可從檔案重算():
    """八欄全驗。**只驗其中幾欄的話，沒驗的那幾欄可以被改成任意值**，
    而一張大表沒有人會逐格核對。"""
    import pandas as pd
    man, tab = _manifests(), _table()
    assert set(tab) == set(man), "表的日期與 manifest 對不上"
    byd = _lite().groupby("date_dir").size().to_dict()
    r4 = _rule4_by_day()
    for name, cells in tab.items():
        d = man[name]
        assert _num(cells[0]) == d["source_files"], name
        assert _num(cells[1]) == d["written_records"], name
        assert _num(cells[2]) == int(byd.get(name, 0)), name
        assert _num(cells[3]) == d["excluded_internal_prompts"], name
        assert cells[4] == f"{100 * d['excluded_internal_prompts'] / d['source_files']:.1f}%", name
        assert _num(cells[5]) == int(r4.get(name, 0)), name
        assert cells[6] == f"{100 * r4.get(name, 0) / d['written_records']:.1f}%", name


def test_只有三天有排除():
    """第二節的三個擷取日與第五節的影響敘述必須一致。"""
    nz = {k: d["excluded_internal_prompts"] for k, d in _manifests().items()
          if d["excluded_internal_prompts"] > 0}
    assert set(nz) == {"2026-07-30", "2026-08-03", "2026-08-04"}
    doc = _doc()
    for k in nz:
        assert f"`{k}`" in doc


def test_缺少_prompt_text_的請求與文件相符():
    """只讀 L1 parquet；不打開原始 JSON，也不輸出任何識別碼。"""
    df = _lite()
    missing = df.prompt_text_sha256.isna()
    responses = missing & df.endpoint.eq("/v1/responses")
    counts = df.loc[responses, "anonymous_user_id"].value_counts()
    assert len(df) == 120_520
    assert int(missing.sum()) == 241
    assert int(df.loc[missing, "prompt_text_len"].isna().sum()) == 241
    assert int((df.loc[missing, "prompt_text_len"] == 0).sum()) == 0
    assert int(responses.sum()) == 174
    assert counts.tolist() == [99, 47, 28]
    assert df.loc[missing, "date_taipei"].nunique() == 12
    assert df.loc[responses, "date_taipei"].nunique() == 8
    accounts = counts.index.tolist()
    activity_days = [
        set(df.loc[df["anonymous_user_id"].eq(account), "date_taipei"].astype(str))
        for account in accounts
    ]
    assert len(set.intersection(*activity_days)) == 0

    from src.classify_lite import prefilter
    assignments, _ = prefilter.build()
    eligible = set(assignments.loc[
        assignments["rule"].eq(prefilter.UNASSIGNED), "prompt_text_sha256"
    ])
    eligible_counts = [
        int((df["anonymous_user_id"].eq(account)
             & df["prompt_text_sha256"].isin(eligible)).sum())
        for account in accounts
    ]
    assert eligible_counts == [69, 40, 206]

    doc = _doc()
    for phrase in ("241 筆", "174 筆", "99／47／28", "69／40／206",
                   "12 天", "8 天", "120,279 = 120,520 − 241"):
        assert phrase in doc


def test_clean_原始檔數只能佐證本機筆數():
    """列檔名而不讀 JSON 內容；不能把檔數說成上游未排除的證據。"""
    root = REPO / "data" / "00_raw"
    paths = list(root.rglob("*.json"))
    if not paths:
        pytest.skip("clean 原始資料不在本機")
    assert len(paths) == 9_937
    assert {p.name for p in root.iterdir() if p.is_file()} == {".gitkeep"}
    doc = _doc()
    assert "3,509 + 6,428 = 9,937" in doc
    assert "不證明上游未排除" in doc


def test_三個擷取日與八個問題在公開檔一致():
    """只核對文件；以記憶體中的錯字突變確認斷言不是空真。

    CLAUDE.md、AGENTS.md 已移出版控，全新 clone 讀不到，所以不再核對它們。
    """
    doc = _doc()

    def check(text):
        assert "三個擷取日" in text
        assert "08-04" in text and "68 筆（0.3%）" in text
        questions = text.split("## 六、待上游回覆的問題", 1)[1].split("## 七、", 1)[0]
        assert len(re.findall(r"(?m)^\d+\. \*\*", questions)) == 8

    check(doc)
    with pytest.raises(AssertionError):
        check(doc.replace("三個擷取日", "兩個擷取日"))


def test_台北日期的換算與實際相符():
    """擷取目錄是 UTC 曆日，受影響的台北日期要另外算——
    這一步錯了，第五節指認的兩天就是錯的兩天。"""
    df, doc = _lite(), _doc()
    for name in ("2026-07-30", "2026-08-03"):
        vc = df[df.date_dir == name].date_taipei.value_counts().sort_index()
        for day, n in vc.items():
            assert re.search(rf"{day}（\s*{n:,} 筆）", doc), f"{name} → {day} {n:,}"


# --- 第三節的論證 -----------------------------------------------------------

def _rule_share():
    if "share" not in _CACHE:
        import pandas as pd
        hits_path = (REPO / "runs" / "2026-09-05T1700_prefilter"
                     / "classify_lite" / "prefilter_hits.parquet")
        if not hits_path.exists():
            pytest.skip("prefilter_hits.parquet 不在本機")
        hits = pd.read_parquet(hits_path)
        rule = dict(zip(hits.prompt_text_sha256, hits.rule))
        _CACHE["ruleset"] = set(rule)
        df = _lite().copy()
        df["rule"] = df.prompt_text_sha256.map(rule)
        _CACHE["share"] = pd.crosstab(df.date_dir, df.rule, normalize="index") * 100
        _CACHE["r4"] = df[df.rule == "4 流程中繼處理"].groupby("date_dir").size().to_dict()
        _CACHE["r3"] = df[df.rule == "3 工具自動發出"].groupby("date_dir").size().to_dict()
    return _CACHE["share"]


def _rule4_by_day():
    _rule_share()
    return _CACHE["r4"]


def test_第三節的規則3佔比兩個分母都與實際相符():
    """排除「上游移除的就是規則 3 那一類」這個候選，靠的是六個數字。

    **兩列都驗。** 只驗其中一個分母的話，另一列可以被改成任意值，
    而它就在旁邊，讀起來像同一組數字的另一種算法——第一版漏驗了那一列。
    """
    import pandas as pd
    share = _rule_share()["3 工具自動發出"]          # 分母：當日有規則判定的列
    counts = _CACHE["r3"]
    man = _manifests()
    aff = ["2026-07-30", "2026-08-03"]
    others = [i for i in share.index if i not in aff]
    doc = _doc()
    wide = pd.Series({k: 100 * counts.get(k, 0) / man[k]["written_records"]
                      for k in share.index})
    rows = {l.split("|")[1].strip(): [c.strip() for c in l.split("|")[2:-1]]
            for l in doc.splitlines()
            if l.startswith("| 當日") and l.count("|") == 5}
    assert len(rows) == 2, f"第三節的兩列分母表解析不出來：{list(rows)}"
    for label, series in ((next(k for k in rows if "written_records" in k), wide),
                          (next(k for k in rows if "前置規則判定" in k), share)):
        cells = rows[label]
        assert cells[0] == f"{series['2026-07-30']:.1f}%", label
        assert cells[1] == f"{series['2026-08-03']:.1f}%", label
        assert cells[2] == f"{series[others].mean():.1f}%", label
        # 論證的方向：受影響兩天**高於**其餘平均。兩個分母都要成立。
        assert series[aff].mean() > series[others].mean(), label


def test_第七節的規則4表逐格與實際相符():
    """**四欄全驗，含兩個分母各自的百分比。**

    第一版的第七節用「有規則判定的列」當分母、第二節那張表用
    `written_records` 當分母，兩個數字並排而沒有標明——由突變檢查撞出來。
    筆數那一欄當時是拿百分比反推的，因此還錯了兩個（13,060 / 7,057，
    實際 13,072 / 7,061）。**反推的數字要當成沒查證過的數字。**
    """
    share = _rule_share()["4 流程中繼處理"]      # 分母：當日有規則判定的列
    counts = _rule4_by_day()
    man = _manifests()
    hot = ["2026-08-04", "2026-08-05", "2026-08-10"]
    doc = _doc()
    for k in hot:
        n = int(counts[k])
        cells = _table(3)[k]
        assert _num(cells[0]) == n, f"{k} 應為 {n:,}：{cells}"
        assert cells[1] == f"{100 * n / man[k]['written_records']:.1f}%", cells
        assert cells[2] == f"{share[k]:.1f}%", cells
    for k in share.index:
        if k not in hot:
            assert share[k] == 0, f"{k} 不再是 0，第七節要改"


def test_文件明寫兩個分母的差額():
    """兩個分母的差就是「沒有前置規則判定的列」。差額變了，兩張表都要重算。"""
    df = _lite()
    _rule_share()
    n = int(df.prompt_text_sha256.map(
        lambda s: s in _CACHE["ruleset"]).eq(False).sum())
    doc = _doc()
    assert f"{n:,} 列沒有前置規則判定" in doc
    assert "並排的數字要標明各自的算法" in doc


# --- 文件本身的性質 ---------------------------------------------------------

def test_manifest_不會被誤當成請求記錄():
    """`extract_lite` 用 rglob 掃全部 json，manifest 也在掃描範圍內。

    它們沒有變成 12 萬列裡的雜訊，靠的是 `request` 是空物件——
    這是巧合不是設計，所以釘住它。哪天 manifest 多了一個 `request` 鍵，
    L1 就會多出 23 列近乎全 null 的資料而不報錯。
    """
    from src import config
    root = config.lite_raw_dir()
    for p in sorted(root.glob("*/*/_prepare_report.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        assert not d.get("request"), p.name


def test_假說明確標為未驗證():
    """第七節那個假說是看到資料之後才形成的。文件不可以讀成已經成立。"""
    doc = _doc()
    assert "**未驗證**" in doc
    assert "本檔不對它下任何結論" in doc
    assert "本檔刻意不執行這三項" in doc


def test_影響清單裡的不受影響項目仍然為零排除():
    """中斷與限流那兩段期間若哪天也有排除，第五節就要改。"""
    man = _manifests()
    for k in ("2026-08-07", "2026-08-10", "2026-08-18", "2026-08-19"):
        assert man[k]["excluded_internal_prompts"] == 0, k


def test_成本數字沒有被_shell_吃掉():
    """本檔一度寫成 `US,749.32`——用未加引號的 heredoc 產生文件時，
    bash 把 `$2` 當成位置參數展開成空字串。

    釘住它是因為**這種壞法讀起來像打字漏字**，而它會出現在任何用
    heredoc 寫檔的地方。金額後面的數字被吃掉，句子仍然通順。
    """
    doc = _doc()
    assert "US$2,749.32" in doc
    assert "US," not in doc
