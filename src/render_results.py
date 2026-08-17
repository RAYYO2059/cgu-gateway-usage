"""把指標輸出渲染進 docs/RESULTS.md 的 AUTOGEN 區塊。

與 render_index 的分工：INDEX.md 講「指標是什麼」（定義、分母、注意事項），
整份都是產生的；RESULTS.md 講「這批資料算出什麼」，只有標記之間是產生的，
標記以外全是手寫的解讀。

三個硬性要求：

1. **只換標記之間**。手寫的散文是這份文件的價值所在，渲染器碰不到它。
   找不到的 KEY 記警告後跳過——靜默忽略的話，文件會少一節而沒有人發現。
2. **冪等**。連續產生兩次必須逐位元組相同，所以輸出裡不得有時間戳或執行序號。
3. **被抑制的值顯示為「—」並附註腳**。空白格會被讀成「沒有資料」，
   那是另一回事——抑制是「算得出來但不給看」。
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import pandas as pd

from src import config
from src.metrics import registry

logger = logging.getLogger(__name__)

RESULTS_PATH = config.DOCS_DIR / "RESULTS.md"
DOCS_DATA_DIR = config.DOCS_DIR / "data"
DOCS_FIGURES_DIR = config.DOCS_DIR / "figures"

_MARKER_TEMPLATE = (
    r"(?P<start><!-- AUTOGEN:{key}:START -->)"
    r".*?"
    r"(?P<end><!-- AUTOGEN:{key}:END -->)"
)
_ANY_MARKER = re.compile(r"<!-- AUTOGEN:([A-Z_0-9]+):(START|END) -->")

# KEY 是文件作者取的簡稱，不等於指標名，所以要明文對照。
# 用「去掉 requests_by_ 前綴」之類的規則自動推導看起來聰明，
# 但作者改一個標記名就會靜默對不上——明文表至少會在啟動時就報錯。
KEY_TO_METRIC = {
    "SCALE": "dataset_scale",
    "ENDPOINT": "requests_by_endpoint",
    "ACCOUNT_TYPE": "requests_by_account_type",
    "CLIENT_TYPE": "requests_by_client_type",
    "MODEL_FAMILY": "requests_by_model_family",
    "STATUS": "status_and_errors",
    "ANOMALY": "anomaly_profile",
    "CACHE_HIT": "cache_hit_by_request_position",
    "TOKEN_INFLATION": "token_inflation_by_client_type",
    "EXPANSION": "turn_expansion_depth",
    "TOOL_RATIO": "thread_tool_message_ratio",
    "TOOL_TYPES": "tool_types_distribution",
    "DEGREE_YEAR": "users_by_degree_and_entry_year",
    "HOUR": "requests_by_hour",
    "MODEL_CONSISTENCY": "model_consistency",
    "USAGE_MISSING": "usage_missing_impact",
    "PROMPT_LENGTH": "prompt_length_distribution",
    "REASONING_EFFORT": "reasoning_effort_distribution",
    "CONTEXT_EXCEEDED": "context_length_exceeded_profile",
}

# 不是指標、但仍要渲染的 run metadata。concentration_summary 在第八步從指標
# 降級成 run 層級產物，因此不會出現在 metrics/ 也不複製到 docs/data/——
# 它描述的是抑制規則本身，不是使用行為。表照渲染，但沒有可連的 csv。
RUN_METADATA_KEYS = {"CONCENTRATION": "concentration_summary"}

# A 形狀：第一欄是標籤不是資料，渲染成表格會變成兩欄的假表。
DEFINITION_LIST_METRICS = {"dataset_scale", "model_consistency"}

# 超過這個列數就折疊，主表只留 HEAD_ROWS 列。
COLLAPSE_THRESHOLD = 12
HEAD_ROWS = 10

# 慣例之外的比例欄名。RATIO_SUFFIXES 認得 _share/_ratio/佔比，
# 但認不得「佔全體請求」這種寫成句子的欄名。
EXTRA_RATIO_COLUMNS = {"佔全體請求", "佔比"}

MISSING = "—"


# ---------------------------------------------------------------------------
# 數值格式化
# ---------------------------------------------------------------------------
def _is_ratio_column(column: str) -> bool:
    name = str(column)
    return (name in EXTRA_RATIO_COLUMNS
            or any(name.endswith(s) for s in registry.RATIO_SUFFIXES))


def _is_count_column(column: str) -> bool:
    name = str(column)
    return name == "n" or name.startswith("n_")


def format_number(value: float, column: str) -> str:
    """比例固定 4 位；計數加千分位；其餘依量級決定小數位。

    分位數欄同時裝得下 0.9868 與 723,731，固定小數位一定有一端很醜，
    所以按量級分段。
    """
    if _is_ratio_column(column):
        return f"{value:.4f}"
    # 整數值就印成整數。母數會以 float 存在（`值` 欄同時裝計數與時間戳），
    # 不特別處理的話 98 會印成 98.0、3 會印成 3.00。
    if _is_count_column(column) or float(value).is_integer():
        return f"{value:,.0f}"
    magnitude = abs(float(value))
    if magnitude >= 100:
        return f"{value:,.0f}"
    if magnitude >= 10:
        return f"{value:.1f}"
    if magnitude >= 1:
        return f"{value:.2f}"
    return f"{value:.4f}"


def format_cell(value, column: str) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return MISSING
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, str):
        text = value.strip()
        return text.replace("|", "\\|") if text else MISSING
    try:
        return format_number(float(value), column)
    except (TypeError, ValueError):
        return str(value).replace("|", "\\|")


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
def _key_columns(frame: pd.DataFrame, name: str | None) -> list[str]:
    """辨識「這一列在講哪一組」的欄位。

    二鍵交叉表不能只看第一欄。users_by_degree_and_entry_year 有 12 列，
    只看 degree 的話全部叫「B 列」「D 列」「M 列」，指不出是哪一列；
    更糟的是 B/9 與 B/10 的 n_users 都是 1，抑制理由字串完全相同，
    於是印出兩行逐字一樣的註腳，看起來像程式壞了。

    指標宣告的 group_by 就是這個定義本身，直接拿來用。沒宣告的
    （長表、run metadata）退回前兩欄：status_and_errors 的 `彙總`
    在第一欄、`(無)` 在第二欄，只看一欄會漏掉總計列。
    """
    spec = registry.REGISTRY.get(name) if name else None
    if spec is not None and spec.group_by:
        declared = [c for c in spec.group_by if c in frame.columns]
        if declared:
            return declared
    return list(frame.columns[:2])


def _row_label(row: pd.Series, columns: list[str]) -> str:
    return " / ".join(str(row[c]) for c in columns)


def _footnotes(frame: pd.DataFrame, name: str | None) -> list[str]:
    """把 suppression_reason 抽成表格下方的註腳。

    直接當一欄印會讓表寬到不能看，而且同一句話會重複幾十列。

    刻意不去重：兩列的理由真的相同時就該印兩行，合併會讓人以為
    只有一列被抑制。列標籤由 _key_columns() 決定，兩列才分得開。
    """
    if registry.REASON_COLUMN not in frame.columns:
        return []
    key_columns = _key_columns(frame, name)
    notes: list[str] = []
    for _, row in frame.iterrows():
        value = row[registry.REASON_COLUMN]
        # 從 csv 讀回來時，未抑制的空字串會變成 float nan。
        # 不先擋掉的話 str(nan) == "nan" 是真值，會產出「已抑制：nan」的假註腳。
        if pd.isna(value):
            continue
        reason = str(value).strip()
        # 豁免不是抑制，不需要註腳——否則 model_family 會拖著 27 條一樣的話。
        if not reason or reason == registry._EXEMPT_REASON:
            continue
        notes.append(
            f"※ `{_row_label(row, key_columns)}` 列的比例已抑制：{reason}")
    return notes


def _rows_to_markdown(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    lines = ["| " + " | ".join(str(c) for c in columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(
            format_cell(row[c], c) for c in columns) + " |")
    return lines


# 彙總／分母／缺值列的標記。這些不是資料，是讀表所需的參照，
# 折疊掉會讓主表的分母與總計消失——而那通常正是讀者要看的第一個數字。
_ANNOTATION_PREFIXES = ("(", "（")
_ANNOTATION_LABELS = ("彙總",)


def _annotation_mask(frame: pd.DataFrame, name: str | None) -> pd.Series:
    """哪些列是彙總列。

    掃的欄位與註腳標籤同一套（_key_columns）：兩處問的是同一個問題——
    「這一列的身分寫在哪幾欄」——所以不該各寫一份判斷。
    只看第一欄會把 status_and_errors 的 `(無)` 漏掉，那列在第二欄。
    """
    def flagged(value) -> bool:
        if not isinstance(value, str):
            return False
        text = value.strip()
        return text.startswith(_ANNOTATION_PREFIXES) or text in _ANNOTATION_LABELS

    mask = pd.Series(False, index=frame.index)
    for column in _key_columns(frame, name):
        mask |= frame[column].map(flagged)
    return mask


def render_table(frame: pd.DataFrame, link: str | None, name: str) -> str:
    """link 控制「完整資料」那行要不要出現；name 一定是指標名，
    因為 _key_columns() 得靠它查 group_by。兩者分開傳，避免
    「不發布 csv」這個決定順手把列標籤也降級成第一欄。
    """
    columns = [c for c in frame.columns if c != registry.REASON_COLUMN]
    notes = _footnotes(frame, name)

    mask = _annotation_mask(frame, name)
    annotation = frame[mask]
    data_rows = frame[~mask]

    lines: list[str] = []
    if len(frame) <= COLLAPSE_THRESHOLD:
        lines += _rows_to_markdown(frame, columns)
    else:
        head = data_rows.head(HEAD_ROWS)
        tail = data_rows.iloc[HEAD_ROWS:]
        lines += _rows_to_markdown(pd.concat([head, annotation]), columns)
        lines += [
            "",
            f"<details><summary>其餘 {len(tail)} 列</summary>",
            "",
        ]
        lines += _rows_to_markdown(tail, columns)
        lines += ["", "</details>"]

    if notes:
        lines.append("")
        lines += notes
    if link:
        lines += ["", f"完整資料：[{link}.csv](data/{link}.csv)"]
    return "\n".join(lines)


def render_definition_list(frame: pd.DataFrame, name: str) -> str:
    """A 形狀專用：第一欄是標籤，不該撐成表格。"""
    label_column, value_columns = frame.columns[0], list(frame.columns[1:])
    note_column = "備註" if "備註" in value_columns else None
    if note_column:
        value_columns.remove(note_column)

    lines: list[str] = []
    for _, row in frame.iterrows():
        values = [format_cell(row[c], c) for c in value_columns]
        values = [v for v in values if v != MISSING]
        note = ""
        if note_column and not pd.isna(row[note_column]):
            note = str(row[note_column]).strip()

        if values:
            line = f"- **{row[label_column]}**：{'／'.join(values)}"
            if note:
                line += f" — {note}"
        else:
            # 只有備註沒有數值的列（ts_first/ts_last）。照通則會印成
            # 「— — 2026-07-21…」，兩個破折號一個是缺值一個是分隔符。
            line = f"- **{row[label_column]}**：{note or MISSING}"
        lines.append(line)
    lines += ["", f"完整資料：[{name}.csv](data/{name}.csv)"]
    return "\n".join(lines)


def render_block(frame: pd.DataFrame, name: str, publish_csv: bool = True) -> str:
    if frame.empty:
        return "_（本次執行沒有資料）_"
    if name in DEFINITION_LIST_METRICS:
        return render_definition_list(frame, name)
    return render_table(frame, name if publish_csv else None, name)


# ---------------------------------------------------------------------------
# 區塊替換
# ---------------------------------------------------------------------------
def replace_block(text: str, key: str, body: str) -> tuple[str, bool]:
    pattern = re.compile(_MARKER_TEMPLATE.format(key=re.escape(key)), re.DOTALL)
    if not pattern.search(text):
        return text, False
    return pattern.sub(
        lambda m: f"{m.group('start')}\n{body}\n{m.group('end')}", text), True


def marker_keys(text: str) -> list[str]:
    seen: list[str] = []
    for key, _ in _ANY_MARKER.findall(text):
        if key not in seen:
            seen.append(key)
    return seen


# ---------------------------------------------------------------------------
# 散文裡的寫死數字
# ---------------------------------------------------------------------------
# 抓百分比、倍數、千分位、小數。刻意抓得寬：漏掉一個要更新的數字，
# 比多列幾個不必更新的糟。
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|倍|筆|列|個|人|字元)?")

# markdown 的有序清單標記。「1. **不能談作息**」裡的 1 不是要核對的數字，
# 每一條列表都報一次只會淹掉真正的數字。
_LIST_MARKER_RE = re.compile(r"^\s*\d+\.\s+")


def prose_number_report(sources: dict[str, str]) -> str:
    """列出 AUTOGEN 標記以外、含有數字的行。

    刻意不做自動比對：判斷「52 倍」該不該改成別的數字需要讀懂上下文，
    程式做不到。做錯了會安靜地改壞手寫段落，比不做危險得多。
    """
    lines = [
        "# 手寫散文裡的數字",
        "",
        "這些數字寫死在 AUTOGEN 標記之外，重跑後不會自動更新，也沒有任何訊號。",
        "本檔只負責列出來，不做比對也不做修改——判斷哪個該改需要讀懂上下文。",
        "請人工逐行核對。",
        "",
    ]
    for label, text in sources.items():
        lines += [f"## {label}", ""]
        inside = False
        hits = 0
        for number, raw in enumerate(text.splitlines(), start=1):
            marker = _ANY_MARKER.search(raw)
            if marker:
                inside = marker.group(2) == "START"
                continue
            if inside:
                continue
            scanned = _LIST_MARKER_RE.sub("", raw)
            found = [m.group(0).strip() for m in _NUMBER_RE.finditer(scanned)
                     if m.group(0).strip()]
            if not found:
                continue
            hits += 1
            lines += [
                f"行 {number}",
                f"  文字：{raw.strip()}",
                f"  數字：{', '.join(found)}",
                "",
            ]
        if not hits:
            lines += ["（此檔的手寫段落沒有數字）", ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 發布
# ---------------------------------------------------------------------------
def copy_metric_csvs(run_id: str) -> list[str]:
    """把指標 csv 複製到 docs/data/。concentration_summary 已降級，不在此列。"""
    source_dir = config.RUNS_DIR / run_id / "metrics"
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in sorted(registry.REGISTRY):
        path = source_dir / f"{name}.csv"
        if not path.exists():
            logger.warning("指標 %s 沒有 csv（%s），略過複製", name, path)
            continue
        shutil.copyfile(path, DOCS_DATA_DIR / f"{name}.csv")
        copied.append(name)

    # 舊指標被移除時，docs/data/ 裡的殘檔會留下來變成過期的公開資料。
    for stale in sorted(DOCS_DATA_DIR.glob("*.csv")):
        if stale.stem not in registry.REGISTRY:
            stale.unlink()
            logger.warning("移除 docs/data/%s：已不是註冊中的指標", stale.name)
    return copied


def load_frame(run_id: str, name: str, run_level: bool = False) -> pd.DataFrame | None:
    run_dir = config.RUNS_DIR / run_id
    path = (run_dir / f"{name}.csv") if run_level else (run_dir / "metrics" / f"{name}.csv")
    if path.exists():
        return pd.read_csv(path, encoding="utf-8-sig")
    if not run_level:
        return None
    # run 層級的產物由 aggregate 寫，`metrics --publish` 單獨執行時本次 run
    # 目錄下不會有。退回最近一次並記警告，比讓文件少一節好——
    # 同 registry.find_concentration() 的理由。
    candidates = sorted(config.RUNS_DIR.glob(f"*/{name}.csv"))
    if not candidates:
        return None
    logger.warning("本次 run 沒有 %s.csv，改用 %s；若聚合結果已過時，"
                   "該區塊的數字可能落後——建議跑 `run all --publish`",
                   name, candidates[-1].parent.name)
    return pd.read_csv(candidates[-1], encoding="utf-8-sig")


def render_results(run_id: str) -> dict:
    """填入 RESULTS.md 的 AUTOGEN 區塊。回傳統計，不 raise。"""
    if not RESULTS_PATH.exists():
        raise FileNotFoundError(
            f"找不到 {RESULTS_PATH}。RESULTS.md 的手寫散文由人撰寫，"
            "渲染器只負責填標記之間的內容，不會憑空產生整份文件。"
        )
    original = RESULTS_PATH.read_text(encoding="utf-8")
    text = original

    present = set(marker_keys(original))
    filled: list[str] = []
    missing_keys: list[str] = []

    for key, name in {**KEY_TO_METRIC, **RUN_METADATA_KEYS}.items():
        run_level = key in RUN_METADATA_KEYS
        if key not in present:
            missing_keys.append(key)
            continue
        frame = load_frame(run_id, name, run_level=run_level)
        if frame is None:
            logger.warning("KEY %s 對應的 %s.csv 不存在，區塊保持原樣", key, name)
            continue
        body = render_block(frame, name, publish_csv=not run_level)
        if run_level:
            body += ("\n\n_此表為 run metadata（`runs/<run_id>/"
                     f"{name}.csv`），描述抑制規則本身而非使用行為，不隨指標 csv 發布。_")
        text, ok = replace_block(text, key, body)
        if ok:
            filled.append(key)

    for key in missing_keys:
        logger.warning("RESULTS.md 找不到 AUTOGEN:%s 標記，該區塊未產生", key)

    # 反向檢查：新增了指標但文件沒跟上，只會表現成「文件少一節」，
    # 而少了什麼從文件本身看不出來。
    mapped = set(KEY_TO_METRIC.values())
    for name in sorted(registry.REGISTRY):
        if name not in mapped:
            logger.warning("指標 %s 沒有對應的 AUTOGEN KEY，不會出現在 RESULTS.md", name)

    unknown = [k for k in present
               if k not in KEY_TO_METRIC and k not in RUN_METADATA_KEYS]
    for key in unknown:
        logger.warning("RESULTS.md 的 AUTOGEN:%s 沒有對應的指標，未填入", key)

    changed = text != original
    if changed:
        RESULTS_PATH.write_text(text, encoding="utf-8")
    return {
        "changed": changed,
        "filled": filled,
        "missing_keys": missing_keys,
        "unknown_keys": unknown,
        "unmapped_metrics": [n for n in sorted(registry.REGISTRY) if n not in mapped],
    }


def write_prose_numbers(run_id: str) -> Path:
    from src import render_index

    sources = {"docs/RESULTS.md": RESULTS_PATH.read_text(encoding="utf-8")}
    if render_index.README_PATH.exists():
        sources["README.md"] = render_index.README_PATH.read_text(encoding="utf-8")
    target = config.RUNS_DIR / run_id / "prose_numbers.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(prose_number_report(sources), encoding="utf-8")
    return target


def run(run_id: str) -> dict:
    """--publish 的進入點：csv → 圖 → RESULTS.md → README → 散文數字清單。"""
    from src import render_figures, render_index

    copied = copy_metric_csvs(run_id)
    figures = render_figures.run(run_id, DOCS_FIGURES_DIR)
    results = render_results(run_id)
    docs = render_index.run()
    prose = write_prose_numbers(run_id)

    logger.info("publish: docs/data/ %d 個 csv、docs/figures/ %d 張圖",
                len(copied), len(figures))
    logger.info("publish: RESULTS.md %s（填入 %d 個區塊）",
                "已更新" if results["changed"] else "無變動", len(results["filled"]))
    logger.info("publish: 手寫數字清單 → %s", prose)
    return {"copied": copied, "figures": figures, "results": results,
            "docs": docs, "prose_numbers": str(prose)}
