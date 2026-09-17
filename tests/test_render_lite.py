"""兩條線的文件渲染不得互相污染。

這裡測的不是「輸出好不好看」，是三件會靜默出錯的事：

1. render_index 只碰 clean 的標記，render_lite 只碰 lite 的標記。
   互相覆寫的話，後跑的那個會把先跑的內容抹掉，而文件看起來仍然完整。
2. render_index 的斷言還在。放寬它會讓 lite 的指標混進 README 的計數。
3. 兩條線同時被 import 時，clean 的表仍然只列 clean 的指標。
   這是實際發生過的事故（「已註冊指標」從 19 變成 23）。
"""

from __future__ import annotations

import pytest

from src import render_index, render_lite
from src.metrics import registry


def test_斷言仍然擋住非_clean():
    """render_index.run() 只收 clean。這道斷言不可放寬。"""
    with pytest.raises(ValueError, match="只能用於 clean"):
        render_index.run(line="lite")


def test_兩條線同時載入時_clean_的表只列_clean():
    """全域 REGISTRY 被污染時，build_metric_table 仍須依 line 篩選。"""
    import src.metrics_lite  # noqa: F401  兩條線都註冊

    assert len(registry.REGISTRY) > len(registry.list_metrics("clean")), (
        "前置條件不成立：lite 指標沒有被註冊進全域 REGISTRY，這個測試就沒在測東西"
    )

    clean_table = render_index.build_metric_table("clean")
    lite_names = [s.name for s in registry.list_metrics("lite")]
    for name in lite_names:
        assert f"`{name}`" not in clean_table, f"clean 的表裡出現了 lite 指標 {name}"

    # 表頭的計數也必須是 clean 的數量，不是全域的。
    n_clean = len(registry.list_metrics("clean"))
    assert f"已註冊指標：{n_clean} 個" in clean_table

    lite_table = render_index.build_metric_table("lite")
    for spec in registry.list_metrics("clean"):
        assert f"`{spec.name}`" not in lite_table, "lite 的表裡出現了 clean 指標"


def test_readme_區塊的計數以_clean_為範圍():
    """README 描述已發佈的 clean 那條線，lite 被 import 也不該改變它的數字。"""
    import src.metrics_lite  # noqa: F401

    block = render_index.build_metrics_block()
    n_clean = len(registry.list_metrics("clean"))
    assert f"已註冊指標**：{n_clean} 個" in block


def test_成本指標全部宣告_line_lite():
    import src.metrics_lite  # noqa: F401

    for name in ("cost_by_unit_lite", "cost_by_model_family_lite",
                 "cost_structure_lite", "cost_coverage_lite"):
        spec = registry.REGISTRY[name]
        assert spec.line == "lite"
        assert spec.denominator, f"{name} 沒有寫分母"
        assert spec.caveat, f"{name} 沒有 caveat"


def test_成本指標的_caveat_帶著四條定價限制():
    """pricing.py docstring 那四條必須跟著每一個成本數字走。

    這四條是「本估算與實際帳單的系統性差距」，不是精度問題。少了任何一條，
    讀者就會把牌價等值成本當成帳單。
    """
    import src.metrics_lite  # noqa: F401

    required = ("service_tier", "區域", "促銷價", "可重現性")
    for name in ("cost_by_unit_lite", "cost_by_model_family_lite",
                 "cost_structure_lite", "cost_coverage_lite"):
        caveat = registry.REGISTRY[name].caveat
        for token in required:
            assert token in caveat, f"{name} 的 caveat 少了「{token}」那一條"


def test_成本的兩個母數都寫進分母欄():
    """成本總額的分母是全部請求，人均的分母是 275 人——兩者不可互推。"""
    import src.metrics_lite  # noqa: F401

    denominator = registry.REGISTRY["cost_by_unit_lite"].denominator
    assert "120,520" in denominator
    assert "275" in denominator
    assert "不可互推" in denominator or "不能互推" in denominator


def test_lite_的_KEY_對照表沒有漏掉指標():
    """新增 lite 指標但忘了加 AUTOGEN KEY，只會表現成「文件少一節」。"""
    import src.metrics_lite  # noqa: F401

    mapped = set(render_lite.KEY_TO_METRIC.values())
    registered = {s.name for s in registry.list_metrics("lite")}
    assert registered - mapped == set(), (
        f"這些 lite 指標沒有對應的 AUTOGEN KEY：{sorted(registered - mapped)}"
    )
    assert mapped - registered == set(), (
        f"KEY_TO_METRIC 指向不存在的指標：{sorted(mapped - registered)}"
    )


def test_金額欄固定兩位小數():
    """通則依量級決定小數位，對錢是錯的：2729.09 會被印成 2,729。"""
    from src import render_results

    assert render_results.format_number(2729.09, "cost_usd") == "2,729.09"
    assert render_results.format_number(88.3, "cost_usd") == "88.30"
    assert render_results.format_number(0.0, "input_cost") == "0.00"
    assert render_results.format_number(10.62, "cost_per_user") == "10.62"
    # 比例欄的規則優先於金額欄，否則 cost_share 會變成兩位小數。
    assert render_results.format_number(0.3865, "cost_share") == "0.3865"
    # per-request 走量級規則：兩位小數會讓它全部變成 0.00。
    assert render_results.format_number(0.008251, "cost_per_request") == "0.0083"


def test_render_lite_不碰_clean_的產出路徑():
    """路徑常數層級的檢查：lite 的 csv 與連結都不得指向 docs/data/。"""
    from src import render_results

    assert render_lite.DOCS_DATA_LITE_DIR != render_results.DOCS_DATA_DIR
    assert render_lite.DATA_LINK_DIR == "data_lite"
    assert render_lite.RESULTS_LITE_PATH != render_results.RESULTS_PATH


def test_render_index_不碰_README_的_lite_區塊():
    """clean 單獨渲染時，HIGHLIGHTS_LITE 必須原封不動。

    否則只跑 clean 的人會把 lite 那一行清成空白，而 README 看起來仍然完整。
    """
    from src import render_index

    original = render_index.README_PATH.read_text(encoding="utf-8")
    body = render_index.build_metrics_block()
    # render_index 只認得三個 KEY，HIGHLIGHTS_LITE 不在其中。
    for key in ("DATA", "METRICS", "HIGHLIGHTS"):
        assert f"AUTOGEN:{key}:START" in original
    assert "AUTOGEN:HIGHLIGHTS_LITE:START" in original
    # build_metrics_block 只給連結，不去讀 docs/data_lite/：讀了的話，
    # 只跑 clean 時 README 會混進一份可能過期的 lite 數字。
    assert "docs/INDEX.md" in body
    assert "data_lite" not in body


def test_lite_摘要區分_uid_與人數():
    """357 個 uid 裡有 82 個是服務憑證，不是人。

    README 是最容易被引用的地方，在那裡寫「357 人」會讓非人實體混進人數，
    與本專案每一個人均分母的處理自相矛盾。
    """
    import inspect

    source = inspect.getsource(render_lite.build_highlights_lite)
    assert "個 uid" in source
    assert "n_service" in source


def test_圖上的金額用跳脫的錢字號():
    """matplotlib 會把成對的 $ 當成 mathtext，把中間的字吃掉。

    "$0.0037  $0.0071" 會被畫成 "0.00370.0071"——**不報錯，只是字少了**。
    單獨一個 $ 剛好沒事，所以這種錯要等到某張圖出現第二個金額才浮出來。
    """
    from src import render_figures_lite

    assert render_figures_lite.USD == r"\$"
    source = inspect_source(render_figures_lite)
    # 圖上的文字一律走 USD 常數，不得直接寫 "$"。
    for bad in ('f"${', '("${'):
        assert bad not in source, f"圖上的文字直接寫了錢字號：{bad}"


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)


def test_逐日指標的日期維度已登記():
    """未登記的維度會被預設納入抑制並警告，逐日的比例就會整欄變 NA。"""
    from src import aggregate_lite

    assert "date_taipei" in aggregate_lite.EXEMPT_DIMENSIONS_LITE
    assert "date_taipei" not in aggregate_lite.CONCENTRATION_DIMENSIONS_LITE


def test_中英對照表由圖上用的字典產生():
    """手寫對照表的話，改了圖上的標籤而忘了改表不會有任何訊號。"""
    from src import render_figures_lite as figs

    assert set(figs.UNIT_EN) == set(figs.UNIT_SHORT), "兩份單位對照的鍵必須一致"
    assert set(figs.SEGMENT_LABELS) == set(figs.SEGMENT_COLORS)
    assert set(figs.SEGMENT_LABELS) == set(figs._SEGMENT_ZH)


def test_錯位圖用計數不用佔比():
    """計數不依賴抑制狀態，佔比會。

    服務憑證的兩個佔比欄現在因非自然人分組豁免而照常公布，但它正是錯位
    最大的一列：畫佔比的話，這一點會隨抑制判定去留。計數不受抑制。
    """
    from src import render_figures_lite

    source = inspect_source(render_figures_lite)
    start = source.index("def traffic_cost_mismatch")
    end = source.index("def build_glossary")
    body = source[start:end]
    assert 'row["n_requests"]' in body and 'row["cost_usd"]' in body
    # 只查實際的欄位存取，不查說明文字——docstring 本來就要提到這兩欄
    # 為什麼不能用。
    for column in ("request_share", "cost_share"):
        assert f'["{column}"]' not in body, f"錯位圖存取了被抑制的 {column}"


def test_欄位白名單寫錯會直接_raise():
    """少幾欄的表看起來仍然完整，所以不能安靜地跳過不存在的欄位。"""
    import pandas as pd
    import pytest as _pytest

    from src import render_results

    frame = pd.DataFrame({"unit": ["A"], "n_users": [1]})
    with _pytest.raises(ValueError, match="沒有欄位"):
        render_results.render_block(frame, "requests_by_unit_lite",
                                    columns=["unit", "does_not_exist"])


def test_表頭改名不影響數值格式化():
    """格式化規則靠欄名認（_usd 結尾兩位小數），所以只能換表頭不能換欄名。

    真的改欄名的話「成本（美元）」不符 MONEY_SUFFIXES，2,749.32 會退回
    量級規則印成 2,749——而那是驗收條件裡的數字。
    """
    import pandas as pd

    from src import render_results

    frame = pd.DataFrame({"unit": ["教職員"], "cost_usd": [1054.95]})
    body = render_results.render_block(
        frame, "cost_by_unit_lite", publish_csv=False,
        rename={"unit": "單位", "cost_usd": "成本（美元）"})
    assert "| 單位 | 成本（美元） |" in body
    assert "1,054.95" in body, "改了表頭之後金額退回量級規則"


def test_裁欄不會弄丟抑制註腳():
    """被抑制的列，註腳是唯一說明「為什麼這格是 —」的地方。

    這裡用服務憑證當合成列；實際資料中它已依非自然人分組豁免，不再被抑制。

    註腳的列標籤靠 spec.group_by 找，而 group_by 宣告的欄位可能不在白名單裡。
    """
    import pandas as pd

    from src import render_results
    from src.metrics import registry

    frame = pd.DataFrame({
        "unit": ["服務憑證"], "unit_type": ["服務憑證"], "n_users": [82],
        "cost_usd": [495.67], "cost_share": [None],
        registry.REASON_COLUMN: ["unit：單人佔 68.9% > 30%"],
    })
    body = render_results.render_block(
        frame, "cost_by_unit_lite", publish_csv=False,
        columns=["n_users", "cost_usd"],  # 白名單裡沒有 unit
        rename=render_lite.OVERVIEW_RENAME)
    assert "已抑制" in body, "裁掉 group_by 的欄位之後註腳消失了"
    assert "服務憑證" in body, "註腳的列標籤不見了"


def test_prose_numbers_涵蓋所有有手寫散文的檔():
    """2026-08-27 踩過：INDEX.md 改成標記式後才有手寫散文，而清單沒跟著加。"""
    from src import render_results

    for name in ("docs/RESULTS.md", "docs/RESULTS_lite.md",
                 "docs/OVERVIEW.md", "docs/INDEX.md", "README.md"):
        assert name in render_results.PROSE_SOURCES, f"{name} 不在掃描清單裡"


def test_總覽的每個_AUTOGEN_KEY_都有對應指標():
    import src.metrics_lite  # noqa: F401
    from src.metrics import registry

    registered = {s.name for s in registry.list_metrics("lite")}
    for key, (name, columns) in render_lite.OVERVIEW_BLOCKS.items():
        assert name in registered, f"{key} 指向不存在的指標 {name}"


def test_被抑制與不適用要印成不同的東西():
    """服務憑證那一列有兩個空格，意思相反：

        request_share       被抑制——算得出來，但依集中度規則不給看
        attributable_share  不適用——它不在可歸屬母體裡，本來就沒有這個值

    印成同一個破折號等於把兩件事併成一件。
    """
    import pandas as pd

    from src import render_results
    from src.metrics import registry

    frame = pd.DataFrame({
        "unit": ["服務憑證", "教職員"],
        "request_share": [None, 0.1869],
        "attributable_share": [None, 0.3726],
        registry.REASON_COLUMN: ["unit：單人佔 68.9% > 30%", ""],
    })
    body = render_results.render_block(
        frame, "requests_by_unit_lite", publish_csv=False,
        suppressed_cells={(0, "request_share")}, na_marker="不適用")
    row = [line for line in body.splitlines()
           if line.startswith("| 服務憑證")][0]
    assert "| — |" in row, "被抑制的格子應該是破折號"
    assert "不適用" in row, "不在母體的格子應該是「不適用」"


def test_sidecar_只記真的被改動的欄位():
    """抑制前就是 NA 的格子不算被抑制。

    分辨的資訊只存在於賦值前那一刻，覆蓋之後就永遠分不出來了。
    """
    import pandas as pd

    from src.metrics import registry

    spec = registry.MetricSpec(
        name="t", question="q", unit="request", source="request",
        denominator="d", caveat=None, needs_dedup=False, group_by=["unit"],
        version="1.0", fn=lambda tables: None, line="lite")
    result = registry.MetricResult(
        data=pd.DataFrame({"unit": ["S"], "a_share": [0.5], "b_share": [None]}),
        n_total=1, n_covered=1, ratio_columns=["a_share", "b_share"])
    rules = pd.DataFrame([{"維度": "unit", "分組值": "S",
                           "below_min_group_size": False, "dominant": True,
                           "n_users": 1, "top1_user_share": 0.9}])
    out = registry.apply_suppression(spec, result, rules,
                                     dimensions=("unit",), exempt=())
    assert out.suppressed[0]["被抑制欄位"] == "a_share", (
        "b_share 抑制前就是 NA，不該被記成被抑制")


def test_預設行為不變():
    """不給 na_marker 時，所有空格仍然是破折號——clean 的輸出靠這個保持不變。"""
    import pandas as pd

    from src import render_results

    frame = pd.DataFrame({"unit": ["A"], "x_share": [None]})
    body = render_results.render_block(frame, "requests_by_unit_lite",
                                       publish_csv=False)
    assert "不適用" not in body
    assert "| — |" in body


def test_被抑制的欄全被裁掉時不印註腳():
    """註腳解釋的是讀者眼前那個 `—`；沒有那個格子，那句話就沒有指涉對象。

    2.2 的 UNIT_CONCENTRATION 區塊只取五個未受抑制的欄，服務憑證那一列
    每一格都是完好的數字。若仍印「本列的比例已抑制」，讀者會以為眼前的
    數字被動過——那是比少印一行嚴重得多的錯。
    """
    import pandas as pd

    from src import render_results
    from src.metrics import registry

    frame = pd.DataFrame({
        "unit": ["服務憑證"],
        "request_share": [None],          # 被抑制
        "top1_account_share": [0.6891],   # 豁免，照印
        registry.REASON_COLUMN: ["unit：單人佔 68.9% > 30%"],
    })
    cells = {(0, "request_share")}

    shown = render_results.render_block(
        frame, "requests_by_unit_lite", publish_csv=False,
        columns=["unit", "request_share"], suppressed_cells=cells)
    assert "已抑制" in shown, "被抑制的欄有印出來時，註腳必須在"

    hidden = render_results.render_block(
        frame, "requests_by_unit_lite", publish_csv=False,
        columns=["unit", "top1_account_share"], suppressed_cells=cells)
    assert "已抑制" not in hidden, "被抑制的欄全被裁掉了，註腳不該出現"
    assert "0.6891" in hidden


def test_沒有_sidecar_時維持原行為():
    """clean 那條路徑不傳 suppressed_cells，註腳照印——寧可多印。"""
    import pandas as pd

    from src import render_results
    from src.metrics import registry

    frame = pd.DataFrame({
        "unit": ["服務憑證"],
        "top1_account_share": [0.6891],
        registry.REASON_COLUMN: ["unit：單人佔 68.9% > 30%"],
    })
    body = render_results.render_block(frame, "requests_by_unit_lite",
                                       publish_csv=False)
    assert "已抑制" in body


def _exempted_service_run(tmp_path, monkeypatch):
    """寫出一個含服務憑證豁免列的 run 目錄，回傳 (run_id, name, frame)。

    走實際路徑：理由字串由 apply_suppression 產生、sidecar 寫進 run 目錄、
    render_lite 從那裡讀回。任何一段自己手寫字串，都測不到契約。
    """
    import json

    import pandas as pd

    import src.metrics_lite  # noqa: F401  讓註腳的列標籤走 group_by
    from src import aggregate_lite, config
    from src.metrics import registry

    name = "requests_by_unit_lite"
    spec = registry.REGISTRY[name]
    service = aggregate_lite.UNIT_SERVICE
    result = registry.MetricResult(
        data=pd.DataFrame({
            "unit": [service, "教職員"],
            "unit_type": [service, "教職員"],
            "n_users": [82, 120],
            "n_accounts": [15, 20],
            "n_requests": [60075, 22524],
            "request_share": [0.4985, 0.1869],
            "attributable_share": [None, 0.3726],   # 不在母體，不是被抑制
            "top1_user_share": [0.6891, 0.0993],
            "top1_account_share": [0.6891, 0.4501],
            "n_users_in_top_account": [1, 29],
        }),
        n_total=2, n_covered=2,
        ratio_columns=["request_share", "attributable_share"])
    rules = pd.DataFrame([{"維度": "unit", "分組值": service,
                           "below_min_group_size": False, "dominant": True,
                           "n_users": 82, "top1_user_share": 0.6891}])
    out = registry.apply_suppression(
        spec, result, rules,
        dimensions=aggregate_lite.CONCENTRATION_DIMENSIONS_LITE,
        exempt=aggregate_lite.EXEMPT_DIMENSIONS_LITE,
        non_person=aggregate_lite.NON_PERSON_GROUPS_LITE)
    assert out.suppressed == [] and len(out.exempted) == 1, (
        "前置條件不成立：這一列沒有走豁免，下面的斷言就沒在測東西")

    run_id = "t_exempted"
    run_dir = tmp_path / run_id / "metrics_lite"
    run_dir.mkdir(parents=True)
    out.data.to_csv(run_dir / f"{name}.csv", index=False,
                    encoding="utf-8-sig", lineterminator="\n")
    (run_dir / f"{name}.exempted.json").write_text(
        json.dumps(out.exempted, ensure_ascii=False), encoding="utf-8",
        newline="\n")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    return run_id, name, render_lite.load_frame(run_id, name)


def _render_overview_block(key, run_id, frame):
    """與 render_overview 對同一個 KEY 的呼叫完全相同。"""
    from src import render_results

    name, columns = render_lite.OVERVIEW_BLOCKS[key]
    return render_results.render_block(
        frame, name, data_dir=render_lite.DATA_LINK_DIR,
        columns=columns, rename=render_lite.OVERVIEW_RENAME,
        suppressed_cells=render_lite._suppressed_cells(run_id, name, frame),
        exempted_cells=render_lite._exempted_cells(run_id, name, frame),
        na_marker=render_lite.NA_MARKER)


def test_非自然人豁免列印數字與豁免註腳且沒有破折號(tmp_path, monkeypatch):
    """版面上三種狀態必須分得開：

        —＋「已抑制」註腳     在 suppressed.json
        數字＋豁免註腳        在 exempted.json
        數字、無註腳          兩份都不在
    """
    from src import aggregate_lite, render_results
    from src.metrics import registry

    run_id, _, frame = _exempted_service_run(tmp_path, monkeypatch)
    body = _render_overview_block("UNIT_MAIN", run_id, frame)
    service = aggregate_lite.UNIT_SERVICE

    row = [line for line in body.splitlines()
           if line.startswith(f"| {service}")][0]
    assert "0.4985" in row, "豁免列的比例沒有印出來"
    assert render_results.MISSING not in body, "豁免列不該出現破折號"
    notes = [line for line in body.splitlines() if line.startswith("※")]
    assert len(notes) == 1, f"應該只有豁免列一條註腳，實際：{notes}"
    assert service in notes[0]
    assert registry._NON_PERSON_MARK in notes[0], "缺豁免註腳"
    assert "依政策不抑制" in notes[0]
    assert "已抑制" not in body, "完好的數字配上了「已抑制」"


def test_沒有比例欄的表不印豁免註腳(tmp_path, monkeypatch):
    """2.2 的 UNIT_CONCENTRATION 不含任何比例欄，豁免的格子一格都沒印出來。

    豁免註腳解釋的是「這個數字為什麼沒有被擋」；表上沒有那個數字，那句話
    就沒有指涉對象——與抑制註腳在欄位被裁掉時不印是同一條規則。
    """
    from src import render_results

    run_id, name, frame = _exempted_service_run(tmp_path, monkeypatch)
    _, columns = render_lite.OVERVIEW_BLOCKS["UNIT_CONCENTRATION"]
    exempted = render_lite._exempted_cells(run_id, name, frame)
    assert exempted, "前置條件不成立：沒有讀到 exempted.json，這個測試就沒在測東西"
    assert not {column for _, column in exempted} & set(columns), (
        "前置條件不成立：這張表含有豁免的欄位")

    body = _render_overview_block("UNIT_CONCENTRATION", run_id, frame)
    assert "0.6891" in body, "表本身要照常印出"
    notes = [line for line in body.splitlines() if line.startswith("※")]
    assert notes == [], f"沒有比例欄的表不該有任何註腳，實際：{notes}"
    assert render_results.MISSING not in body
