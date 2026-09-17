"""lite 這條線的文件渲染器。

**為什麼是獨立模組，而不是放寬 render_index 的斷言**

``render_index.run()`` 的 ``assert line == "clean"`` 擋的是一個真的發生過的
事故：該模組有數處直接讀全域 ``registry.REGISTRY``，lite 的指標一旦被 import，
「已註冊指標」就從 19 變成 23，而 INDEX.md 與 README.md 會被一起改寫——
那兩個檔屬於已發佈的 clean 版本。

斷言留著，另寫這個模組。兩邊的分工是：

    render_index  填 INDEX.md 的 METRICS_CLEAN / SUPPRESSION_CLEAN
                  填 README.md 的三個區塊
    render_lite   填 INDEX.md 的 METRICS_LITE / SUPPRESSION_LITE
                  填 RESULTS_lite.md 的所有區塊
                  複製 lite 的 csv 到 docs/data_lite/

兩者都只換自己的標記，不覆寫整份文件，所以先後順序不影響結果。

表格的渲染邏輯共用 ``render_results`` 的函式（``render_block`` 等），只是把
csv 連結指到 ``data_lite/``。抄一份的話兩邊的格式化規則遲早漂移，而漂移的
症狀是「同一種表在兩份文件裡長得不一樣」，沒有人會立刻聯想到是兩份程式碼。
"""

from __future__ import annotations

import logging
import shutil

import pandas as pd

from src import config
from src.metrics import registry

logger = logging.getLogger(__name__)

LINE = "lite"
RESULTS_LITE_PATH = config.DOCS_DIR / "RESULTS_lite.md"
INDEX_PATH = config.DOCS_DIR / "INDEX.md"
DOCS_DATA_LITE_DIR = config.DOCS_DIR / "data_lite"
DATA_LINK_DIR = "data_lite"

# KEY 是文件作者取的簡稱，不等於指標名，所以要明文對照——理由同
# render_results.KEY_TO_METRIC：用前綴規則自動推導看起來聰明，但作者改一個
# 標記名就會靜默對不上。
KEY_TO_METRIC = {
    "UNIT": "requests_by_unit_lite",
    "USERS": "users_by_unit_lite",
    "UNMAPPED": "unmapped_dept_codes_lite",
    "UNIT_COVERAGE": "unit_coverage_lite",
    "COST_UNIT": "cost_by_unit_lite",
    "COST_MODEL": "cost_by_model_family_lite",
    "COST_STRUCTURE": "cost_structure_lite",
    "COST_COVERAGE": "cost_coverage_lite",
    "COST_DAY": "cost_by_day_lite",
}

# 空表要說的話。render_results.render_block() 對空表印「本次執行沒有資料」，
# 那句話對**監測指標**是錯的：unmapped_dept_codes_lite 空掉不是沒跑出資料，
# 是缺口補完了。兩者在文件上長得一樣，但一個要人去查、一個不用。
#
# 這些表清空之後**留著不移除**，理由與 unpriced_no_table 相同：它們量的是
# 「現在有沒有問題」，不是「當初有幾個問題」。移掉的話下次冒出新代碼時，
# 沒有任何東西會發現。
EMPTY_BLOCK_MESSAGES = {
    "unmapped_dept_codes_lite":
        "_沒有對不到學院的系代號：全部學生 uid 都歸到了學院。_"
        "\n\n"
        "_這張表留著而不是移除——它是監測用的，下次出現新的系代號時"
        "會重新有值。_",
}

OVERVIEW_PATH = config.DOCS_DIR / "OVERVIEW.md"

# 總覽嵌入的表：(指標名, 欄位白名單)。白名單 None = 全欄。
#
# **為什麼要裁欄而不是手抄。** 總覽的讀者不會點開 csv，主表只給連結等於沒交；
# 但完整的 requests_by_unit_lite 有 9 欄、渲染出來 144 格寬，手機版必爆。
# 手抄成精簡表則會產生第三份副本，和 csv、和 RESULTS_lite.md 各自漂移，
# 而且沒有任何訊號（prose_numbers 只列不比對）。裁欄走渲染器兩者都避開：
# 數字仍由管線產生，被裁掉的欄位仍在連結的 csv 裡。
OVERVIEW_BLOCKS = {
    # request_share 與 attributable_share 並列，是為了讓「分母不同」變成
    # 看得見的事實而不是靠散文解釋：前者的分母是全部 120,520 筆（服務憑證
    # 也有值，六列合計約 100%），後者只有可歸屬到個人的 60,445 筆（服務憑證
    # 不在其內，印「不適用」，其餘五列合計 100%）。同一張表上並排，讀者不會
    # 把兩欄的數字讀成同一個分母。
    "UNIT_MAIN": ("requests_by_unit_lite",
                  ["unit", "unit_type", "n_users", "n_requests",
                   "request_share", "attributable_share",
                   # 兩個「最大佔比」欄必須相鄰：醫學院那列剛好相等
                   # （最大的那個遮罩帳號底下只有 1 個 uid），並排時
                   # 讀者會自己看出兩欄在量不同的東西。拆開就沒了。
                   # 且 top1_user_share 是抑制的觸發條件，與表下的抑制
                   # 註腳並排出現，規則才自證。
                   "top1_user_share", "top1_account_share"]),
    # 2.2 專用。與 UNIT_MAIN 同一個指標、不同切面：那張表回答「各單位用了
    # 多少」，這張回答「那些數字背後是幾個人」。刻意不把兩個新欄併進
    # UNIT_MAIN——併進去是 10 欄 135 格，而 2.2 要的是拿掉請求量之後只剩
    # 身分結構的窄表（5 欄 74 格），兩者的讀法不一樣。
    "UNIT_CONCENTRATION": ("requests_by_unit_lite",
                           ["unit", "n_users", "n_accounts",
                            "n_users_in_top_account", "top1_account_share"]),
    "COST_STRUCTURE_MAIN": ("cost_structure_lite", None),
    "COST_UNIT_MAIN": ("cost_by_unit_lite",
                       ["unit", "n_users", "cost_usd", "cost_share",
                        "cost_per_request"]),
    # 拿掉「備註」欄：那四行是在解釋四種計價狀態的差別，放進散文比放進表格
    # 更能講清楚，而它們讓這張表撐到 184 格寬（手機必爆）。表格只留數字。
    "COST_COVERAGE_MAIN": ("cost_coverage_lite",
                           ["pricing_status", "n_requests", "request_share",
                            "tokens", "token_share", "cost_usd"]),
}

# csv 欄名 → 總覽的顯示名。**只換表頭，csv 本身不動**——下游程式在讀它。
#
# 兩個刻意的用字：
#   n_users            → 「帳號數」不是「人數」。那一欄數的是 uid，而服務憑證
#                        那 82 個 uid 不是人；叫人數會讓 357 被當成人。
#   top1_account_share → 保留「遮罩」二字。拿掉的話讀者會以為是某個具體的人，
#                        而它近似的是「一個班」。
OVERVIEW_RENAME = {
    "unit": "單位",
    "unit_type": "類別",
    "n_users": "帳號數",
    "n_requests": "請求數",
    "request_share": "請求佔比",
    "attributable_share": "佔可歸屬流量",
    "top1_user_share": "最大單一帳號佔比",
    "top1_account_share": "最大遮罩帳號佔比",
    # 「遮罩帳號數」不是「帳號數」：n_users 已經佔用了「帳號數」這個詞
    # （它數的是 uid），兩欄同表並排時名字必須看得出量的是不同東西。
    "n_accounts": "遮罩帳號數",
    "n_users_in_top_account": "最大帳號下帳號數",
    "is_single_dept_college": "單系學院",
    "user_share": "人數佔比",
    "mean_requests": "平均請求數",
    "cost_usd": "成本（美元）",
    "cost_share": "成本佔比",
    "cost_per_request": "每筆成本",
    "cost_per_user": "每帳號成本",
    "tokens": "token 數",
    "token_share": "token 佔比",
    "pricing_status": "計價狀態",
}

# 圖與對照表不是指標 csv，另外處理。GLOSSARY 由 render_figures_lite 的對照
# 字典產生，FIGURES 是四張圖的嵌入區塊。
DOCS_FIGURES_LITE_DIR = config.DOCS_DIR / "figures_lite"


# 總覽的表把兩種空格分開印。RESULTS_lite.md 只在 NA_MARKER_METRICS 那兩張表
# 套用：服務憑證那一列的空格是「不在母體」。其餘表的空格多半是「算不出來」
# （例如未計價模型的金額），印成「不適用」是錯的，所以維持 —。
NA_MARKER = "不適用"
NA_MARKER_METRICS = frozenset({"requests_by_unit_lite", "cost_by_unit_lite"})


def _sidecar_cells(run_id: str, name: str, frame, kind: str,
                   column_key: str) -> set:
    import json

    path = config.RUNS_DIR / run_id / "metrics_lite" / f"{name}.{kind}.json"
    if not path.exists():
        return set()
    cells = set()
    for item in json.loads(path.read_text(encoding="utf-8")):
        dimension, value = item["維度"], str(item["分組值"])
        columns = [c for c in item[column_key].split(",") if c]
        if dimension not in frame.columns:
            continue
        mask = frame[dimension].astype(str) == value
        for index in frame.index[mask]:
            for column in columns:
                cells.add((index, column))
    return cells


def _suppressed_cells(run_id: str, name: str, frame) -> set:
    """哪些 (列, 欄) 是真的被抑制的。

    來源是 sidecar 的「被抑制欄位」，而那一欄只列 apply_suppression **真的
    改動過**的欄位——抑制前就是 NA 的格子不算。少了這個區分，某單位的
    request_share（被抑制）與服務憑證的 attributable_share（不在母體）會印成
    同一個破折號，而它們的意思相反。
    """
    return _sidecar_cells(run_id, name, frame, "suppressed", "被抑制欄位")


def _exempted_cells(run_id: str, name: str, frame) -> set:
    """哪些 (列, 欄) 是非自然人豁免、數字照常公布的。

    來源是 exempted.json 的「未抑制欄位」。註腳層靠它判斷表上有沒有印出
    豁免的格子——與 _suppressed_cells 對抑制註腳的作用相同。
    """
    return _sidecar_cells(run_id, name, frame, "exempted", "未抑制欄位")


def _import_lite_metrics() -> None:
    """註冊 lite 指標。放在函式裡而不是模組頂層。

    模組頂層 import 的話，任何人 ``import src.render_lite``（例如為了讀
    KEY_TO_METRIC）就會順帶污染全域 REGISTRY，而污染的後果落在 clean 的
    文件上。放在函式裡，只有真的要渲染 lite 時才發生。
    """
    import src.metrics_lite  # noqa: F401


def copy_metric_csvs(run_id: str) -> list[str]:
    """lite 的指標 csv → docs/data_lite/。

    與 clean 的 docs/data/ 分開放，不是為了整齊：兩條線有同名概念但不同母體
    （requests_by_unit_lite 與 clean 沒有對應表，但將來可能有），混在一個
    目錄裡，讀者拿到 csv 時無從判斷它屬於哪批資料、哪段期間。
    """
    _import_lite_metrics()
    source_dir = config.RUNS_DIR / run_id / "metrics_lite"
    DOCS_DATA_LITE_DIR.mkdir(parents=True, exist_ok=True)
    names = [s.name for s in registry.list_metrics(LINE)]

    copied: list[str] = []
    for name in names:
        path = source_dir / f"{name}.csv"
        if not path.exists():
            logger.warning("指標 %s 沒有 csv（%s），略過複製", name, path)
            continue
        shutil.copyfile(path, DOCS_DATA_LITE_DIR / f"{name}.csv")
        copied.append(name)

    # 舊指標被移除時，殘檔會留下來變成過期的公開資料。只清 lite 的名單，
    # 不碰 docs/data/——那是 clean 的地盤。
    for stale in sorted(DOCS_DATA_LITE_DIR.glob("*.csv")):
        if stale.stem not in names:
            stale.unlink()
            logger.warning("移除 docs/data_lite/%s：已不是註冊中的 lite 指標",
                           stale.name)
    return copied


def load_frame(run_id: str, name: str) -> pd.DataFrame | None:
    path = config.RUNS_DIR / run_id / "metrics_lite" / f"{name}.csv"
    if path.exists():
        return pd.read_csv(path, encoding="utf-8-sig")
    return None


def render_results_lite(run_id: str) -> dict:
    """填入 RESULTS_lite.md 的 AUTOGEN 區塊。回傳統計，不 raise。"""
    from src import render_index, render_results

    _import_lite_metrics()
    if not RESULTS_LITE_PATH.exists():
        raise FileNotFoundError(
            f"找不到 {RESULTS_LITE_PATH}。散文由人撰寫，"
            "渲染器只負責填標記之間的內容，不會憑空產生整份文件。"
        )
    original = RESULTS_LITE_PATH.read_text(encoding="utf-8")
    text = original

    present = set(render_results.marker_keys(original))
    filled: list[str] = []
    missing_keys: list[str] = []

    # GLOSSARY 不是指標，由對照字典產生——手寫的話改了圖上的標籤而忘了改表，
    # 讀者會對不上，而那種錯沒有任何訊號。
    if "GLOSSARY" in present:
        from src import render_figures_lite

        text, ok = render_results.replace_block(
            text, "GLOSSARY", render_figures_lite.build_glossary(run_id))
        if ok:
            filled.append("GLOSSARY")
    else:
        missing_keys.append("GLOSSARY")

    for key, name in KEY_TO_METRIC.items():
        if key not in present:
            missing_keys.append(key)
            continue
        frame = load_frame(run_id, name)
        if frame is None:
            logger.warning("KEY %s 對應的 %s.csv 不存在，區塊保持原樣", key, name)
            continue
        if frame.empty and name in EMPTY_BLOCK_MESSAGES:
            # 連結照樣給。csv 只剩表頭，但「表還在」這件事要看得見——
            # 只留一句話說留著、卻沒有東西可以點，讀者無從分辨它是被留著
            # 還是被拿掉了。
            body = (EMPTY_BLOCK_MESSAGES[name] + "\n\n"
                    + f"完整資料：[{name}.csv]({DATA_LINK_DIR}/{name}.csv)")
        else:
            body = render_results.render_block(
                frame, name, data_dir=DATA_LINK_DIR,
                suppressed_cells=_suppressed_cells(run_id, name, frame),
                exempted_cells=_exempted_cells(run_id, name, frame),
                na_marker=NA_MARKER if name in NA_MARKER_METRICS else None)
        text, ok = render_results.replace_block(text, key, body)
        if ok:
            filled.append(key)

    for key in missing_keys:
        logger.warning("RESULTS_lite.md 找不到 AUTOGEN:%s 標記，該區塊未產生", key)

    # 反向檢查：新增了指標但文件沒跟上，只會表現成「文件少一節」，
    # 而少了什麼從文件本身看不出來。
    mapped = set(KEY_TO_METRIC.values())
    unmapped = [s.name for s in registry.list_metrics(LINE)
                if s.name not in mapped]
    for name in unmapped:
        logger.warning("指標 %s 沒有對應的 AUTOGEN KEY，不會出現在 RESULTS_lite.md",
                       name)

    unknown = [k for k in present
               if k not in KEY_TO_METRIC and k != "GLOSSARY"]
    for key in unknown:
        logger.warning("RESULTS_lite.md 的 AUTOGEN:%s 沒有對應的指標，未填入", key)

    render_index.check_bold_delimiters(text, "docs/RESULTS_lite.md")

    changed = text != original
    if changed:
        RESULTS_LITE_PATH.write_text(text, encoding="utf-8", newline="\n")
    return {"changed": changed, "filled": filled, "missing_keys": missing_keys,
            "unknown_keys": unknown, "unmapped_metrics": unmapped}


def render_overview(run_id: str) -> dict:
    """填入 OVERVIEW.md 的 AUTOGEN 區塊。檔案不存在就跳過，不報錯。"""
    from src import render_index, render_results

    _import_lite_metrics()
    if not OVERVIEW_PATH.exists():
        logger.info("沒有 %s，跳過總覽渲染", OVERVIEW_PATH.name)
        return {"changed": False, "filled": [], "missing_keys": []}

    original = OVERVIEW_PATH.read_text(encoding="utf-8")
    text = original
    present = set(render_results.marker_keys(original))
    filled, missing = [], []

    for key, (name, columns) in OVERVIEW_BLOCKS.items():
        if key not in present:
            missing.append(key)
            continue
        frame = load_frame(run_id, name)
        if frame is None:
            logger.warning("KEY %s 對應的 %s.csv 不存在，區塊保持原樣", key, name)
            continue
        body = render_results.render_block(
            frame, name, data_dir=DATA_LINK_DIR,
            columns=columns, rename=OVERVIEW_RENAME,
            suppressed_cells=_suppressed_cells(run_id, name, frame),
            exempted_cells=_exempted_cells(run_id, name, frame),
            na_marker=NA_MARKER)
        text, ok = render_results.replace_block(text, key, body)
        if ok:
            filled.append(key)

    for key in missing:
        logger.warning("OVERVIEW.md 找不到 AUTOGEN:%s 標記，該區塊未產生", key)
    unknown = [k for k in present if k not in OVERVIEW_BLOCKS]
    for key in unknown:
        logger.warning("OVERVIEW.md 的 AUTOGEN:%s 沒有對應的指標，未填入", key)

    render_index.check_bold_delimiters(text, "docs/OVERVIEW.md")
    changed = text != original
    if changed:
        OVERVIEW_PATH.write_text(text, encoding="utf-8", newline="\n")
    return {"changed": changed, "filled": filled, "missing_keys": missing}


def render_index_lite() -> bool:
    """填入 INDEX.md 的兩個 lite 區塊。**不碰 clean 的區塊。**"""
    from src import render_index

    _import_lite_metrics()
    if not INDEX_PATH.exists():
        raise FileNotFoundError(
            f"找不到 {INDEX_PATH}。INDEX.md 的導覽由人撰寫，"
            "渲染器只負責填標記之間的內容。"
        )
    original = INDEX_PATH.read_text(encoding="utf-8")
    text = render_index.replace_block(
        original, "METRICS_LITE",
        render_index.build_metric_table(LINE), "docs/INDEX.md")
    text = render_index.replace_block(
        text, "SUPPRESSION_LITE",
        render_index.build_suppression_block(LINE), "docs/INDEX.md")
    render_index.check_bold_delimiters(text, "docs/INDEX.md")
    if text == original:
        return False
    INDEX_PATH.write_text(text, encoding="utf-8", newline="\n")
    return True


def build_highlights_lite(run_id: str) -> str:
    """README 的 lite 那一行：規模 + 成本總額 + 快取讀寫佔比。

    **資料來源是 runs/<run_id>/metrics_lite/，不是 docs/data_lite/。**
    差別在只跑 clean 的時候：讀 docs/ 的話，render_index 就得依賴 lite 的
    已發佈產物，而那個產物可能比 clean 舊好幾輪，README 會變成半舊半新。
    現在的分工是「誰的產物誰填」——這個函式只在 render_lite 執行時被呼叫，
    clean 單獨跑時這個區塊原封不動。
    """
    structure = load_frame(run_id, "cost_structure_lite")
    coverage = load_frame(run_id, "unit_coverage_lite")
    users = load_frame(run_id, "users_by_unit_lite")
    if structure is None:
        return "_（尚未執行過 lite 指標，暫無摘要）_"

    rows = structure.set_index("項目")
    total_cost = float(rows.loc["總金額", "cost_usd"])
    n_requests = int(rows.loc["總金額", "n_requests"])
    cache_share = (float(rows.loc["快取讀", "佔比"])
                   + float(rows.loc["快取寫", "佔比"]))

    from src import aggregate_lite, extract_lite

    # 期間不寫死（會隨批次變），從資料算。
    n_days = extract_lite.load_dataset()["date_taipei"].astype(str).nunique()

    # **uid 數與人數要分開講。** 全部 uid 有 357 個，但其中 82 個是服務憑證，
    # 那不是人。只寫「357 人」會讓非人實體混進人數，而這個專案的每一個人均
    # 分母都排除它們——README 是最容易被引用的地方，不能在這裡自打嘴巴。
    all_users = pd.read_parquet(aggregate_lite.USER_LITE_PATH)
    n_uid = len(all_users)
    n_people = int(users["n_users"].sum()) if users is not None else 0
    n_service = n_uid - n_people

    return (
        f"- **規模與成本**（lite，{n_days} 天／{n_requests:,} 筆／"
        f"{n_uid} 個 uid，其中 {n_people} 人、{n_service} 個服務憑證）。"
        f"牌價等值成本合計 **US${total_cost:,.2f}**，其中快取讀寫合計佔 "
        f"**{100 * cache_share:.1f}%**——機制的解釋見 "
        "[docs/RESULTS.md](docs/RESULTS.md)。"
    )


def render_readme_lite(run_id: str) -> bool:
    """只填 README 的 HIGHLIGHTS_LITE，其餘區塊一律不碰。"""
    from src import render_index

    path = render_index.README_PATH
    original = path.read_text(encoding="utf-8")
    text = render_index.replace_block(
        original, "HIGHLIGHTS_LITE", build_highlights_lite(run_id), "README.md")
    render_index.check_bold_delimiters(text, "README.md")
    if text == original:
        return False
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def run(run_id: str) -> dict:
    """lite 的 --publish 進入點。

    填四個地方：docs/data_lite/、RESULTS_lite.md、INDEX.md 的兩個 lite 區塊、
    README 的 HIGHLIGHTS_LITE。

    **不碰 docs/data/、docs/RESULTS.md、docs/figures/，也不碰 README 的其他
    三個區塊。** 那些是 clean 的產物，由 render_results.run() 負責。
    """
    copied = copy_metric_csvs(run_id)
    from src import render_figures_lite

    figures = render_figures_lite.run(run_id, DOCS_FIGURES_LITE_DIR)
    results = render_results_lite(run_id)
    overview = render_overview(run_id)
    index_changed = render_index_lite()
    readme_changed = render_readme_lite(run_id)

    logger.info("publish lite: docs/data_lite/ %d 個 csv、docs/figures_lite/ %d 張圖",
                len(copied), len(figures))
    logger.info("publish lite: RESULTS_lite.md %s（填入 %d 個區塊）",
                "已更新" if results["changed"] else "無變動", len(results["filled"]))
    logger.info("publish lite: INDEX.md 的 lite 區塊 %s",
                "已更新" if index_changed else "無變動")
    logger.info("publish lite: OVERVIEW.md %s（填入 %d 個區塊）",
                "已更新" if overview["changed"] else "無變動",
                len(overview["filled"]))
    logger.info("publish lite: README 的 HIGHLIGHTS_LITE %s",
                "已更新" if readme_changed else "無變動")
    return {"copied": copied, "figures": figures, "results": results,
            "overview": overview, "index_changed": index_changed,
            "readme_changed": readme_changed}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    candidates = sorted(config.RUNS_DIR.glob("*/metrics_lite"))
    if not candidates:
        logger.error("找不到任何 runs/*/metrics_lite，請先執行 "
                     "python -m src.run metrics --line lite")
        return 1
    run(candidates[-1].parent.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
