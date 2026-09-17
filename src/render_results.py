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

# 金額欄一律兩位小數。通則（依量級決定小數位）對錢是錯的：$2,729.09 會被印成
# 2,729、$88.30 會被印成 88.3，而驗收條件是「成本總額仍是 $2,729.09」——
# 讀者看到的數字必須就是那個數字，不能是四捨五入後長得差不多的另一個。
# cost_per_request 不在此列：它遠小於 1，兩位小數會全部變成 0.00，
# 走量級規則反而剛好（0.0083）。
MONEY_SUFFIXES = ("_usd", "_cost")
MONEY_COLUMNS = {"cost_per_user"}

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


def _is_money_column(column: str) -> bool:
    name = str(column)
    return name in MONEY_COLUMNS or name.endswith(MONEY_SUFFIXES)


def format_number(value: float, column: str) -> str:
    """比例固定 4 位；計數加千分位；其餘依量級決定小數位。

    分位數欄同時裝得下 0.9868 與 723,731，固定小數位一定有一端很醜，
    所以按量級分段。
    """
    if _is_ratio_column(column):
        return f"{value:.4f}"
    if _is_money_column(column):
        return f"{value:,.2f}"
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


def _shows_any(index, displayed: list[str] | None, cells: set | None) -> bool:
    """這一列在表上有沒有印出 cells 裡的任何一格。

    displayed 或 cells 缺一就回 True：沒有 sidecar 可查時寧可多印註腳。
    抑制與豁免兩種註腳共用這一支——兩者解釋的都是「讀者眼前那一格」，
    那一格被裁掉時，兩種註腳同樣失去指涉對象。
    """
    if displayed is None or cells is None:
        return True
    return any((index, c) in cells for c in displayed)


def _footnotes(frame: pd.DataFrame, name: str | None,
               displayed: list[str] | None = None,
               suppressed_cells: set | None = None,
               exempted_cells: set | None = None) -> list[str]:
    """把 suppression_reason 抽成表格下方的註腳。

    直接當一欄印會讓表寬到不能看，而且同一句話會重複幾十列。

    刻意不去重：兩列的理由真的相同時就該印兩行，合併會讓人以為
    只有一列被抑制。列標籤由 _key_columns() 決定，兩列才分得開。

    displayed 與 suppressed_cells 一起給的時候，只有「這張表真的印出了某個
    被抑制的格子」的列才會產生註腳。註腳的功能是解釋讀者眼前那個 `—`；
    白名單把所有被抑制的欄都裁掉之後，那句話就沒有指涉對象了——讀者看到
    一整列完好的數字配一句「本列的比例已抑制」，只會以為眼前的數字被動過。
    兩者缺一（clean 那條路徑不傳 sidecar）就維持原行為，寧可多印。

    非自然人豁免的註腳走同一個檢查，查的是 exempted_cells（exempted.json 的
    「未抑制欄位」）。它解釋的是「這個數字為什麼沒有被擋」，表上沒有那個
    數字時同樣不該出現。
    """
    if registry.REASON_COLUMN not in frame.columns:
        return []
    key_columns = _key_columns(frame, name)
    notes: list[str] = []
    for index, row in frame.iterrows():
        value = row[registry.REASON_COLUMN]
        # 從 csv 讀回來時，未抑制的空字串會變成 float nan。
        # 不先擋掉的話 str(nan) == "nan" 是真值，會產出「已抑制：nan」的假註腳。
        if pd.isna(value):
            continue
        reason = str(value).strip()
        if not reason:
            continue

        # 一列可能同時帶多個維度的理由（group_by 宣告兩個維度時），而它們的
        # 狀態可以不同：一個被抑制、另一個依政策豁免。所以逐段分類，不要拿
        # 整串去比對——那會讓其中一種狀態被另一種蓋掉。
        parts = [p for p in reason.split("；") if p.strip()]
        suppressed_parts = [
            p for p in parts
            if p != registry._EXEMPT_REASON
            and registry._NON_PERSON_MARK not in p]
        non_person_parts = [p for p in parts
                            if registry._NON_PERSON_MARK in p]

        # 非自然人豁免：那一列的數字**印得出來**，不在 suppressed.json 裡，
        # 所以查的是 exempted_cells。註腳的語氣也相反：解釋的是「這個數字
        # 為什麼沒有被擋」。
        if _shows_any(index, displayed, exempted_cells):
            for part in non_person_parts:
                notes.append(f"※ `{_row_label(row, key_columns)}` 列{part}")

        if not _shows_any(index, displayed, suppressed_cells):
            continue
        # 維度層豁免不是抑制，不需要註腳——否則 model_family 會拖著 27 條一樣的話。
        if not suppressed_parts:
            continue
        notes.append(
            f"※ `{_row_label(row, key_columns)}` 列的比例已抑制："
            f"{'；'.join(suppressed_parts)}")
    return notes


def _rows_to_markdown(frame: pd.DataFrame, columns: list[str],
                      rename: dict | None = None,
                      suppressed_cells: set | None = None,
                      na_marker: str | None = None) -> list[str]:
    """rename **只換表頭**，不改欄名本身。

    格式化規則是靠欄名認的（_share 結尾算比例、_usd 結尾算金額），
    真的把欄位改名的話「成本（美元）」就不再符合 MONEY_SUFFIXES，
    金額會退回量級規則印成 2,749 而不是 2,749.32。
    """
    rename = rename or {}
    lines = ["| " + " | ".join(str(rename.get(c, c)) for c in columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    suppressed_cells = suppressed_cells or set()
    for index, row in frame.iterrows():
        cells = []
        for column in columns:
            value = row[column]
            blank = value is None or (not isinstance(value, str)
                                      and pd.isna(value))
            if blank and na_marker is not None:
                # 空格有兩種意思，印成同一個破折號等於把它們併成一件事：
                #   被抑制   算得出來，但依集中度規則不給看
                #   不適用   本來就沒有這個值（該列不在那個母體裡）
                cells.append(MISSING if (index, column) in suppressed_cells
                             else na_marker)
            else:
                cells.append(format_cell(value, column))
        lines.append("| " + " | ".join(cells) + " |")
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


def render_table(frame: pd.DataFrame, link: str | None, name: str,
                 data_dir: str = "data", columns: list | None = None,
                 rename: dict | None = None,
                 suppressed_cells: set | None = None,
                 na_marker: str | None = None,
                 exempted_cells: set | None = None) -> str:
    """link 控制「完整資料」那行要不要出現；name 一定是指標名，
    因為 _key_columns() 得靠它查 group_by。兩者分開傳，避免
    「不發布 csv」這個決定順手把列標籤也降級成第一欄。
    """
    if columns is None:
        display = [c for c in frame.columns if c != registry.REASON_COLUMN]
    else:
        unknown = [c for c in columns if c not in frame.columns]
        if unknown:
            raise ValueError(
                f"{name} 沒有欄位 {unknown}；可用的是 {list(frame.columns)}。"
                "欄位白名單寫錯只會少幾欄，表看起來仍然完整，所以直接 raise")
        display = [c for c in columns if c != registry.REASON_COLUMN]
    columns = display
    # 註腳用**完整的** frame 算：_key_columns() 靠 spec.group_by 找列標籤，
    # 而 group_by 宣告的欄位可能不在白名單裡。註腳不可因為**標籤欄**被裁掉
    # 而消失——那是唯一說明「為什麼這格是 —」的地方。
    #
    # 但被抑制的欄整批被裁掉時就相反：那句話沒有任何指涉對象了。
    # displayed 傳進去讓 _footnotes 自己判斷，見那邊的說明。
    notes = _footnotes(frame, name, display, suppressed_cells, exempted_cells)

    mask = _annotation_mask(frame, name)
    annotation = frame[mask]
    data_rows = frame[~mask]

    lines: list[str] = []
    if len(frame) <= COLLAPSE_THRESHOLD:
        lines += _rows_to_markdown(frame, columns, rename,
                                   suppressed_cells, na_marker)
    else:
        head = data_rows.head(HEAD_ROWS)
        tail = data_rows.iloc[HEAD_ROWS:]
        lines += _rows_to_markdown(pd.concat([head, annotation]), columns,
                                   rename, suppressed_cells, na_marker)
        lines += [
            "",
            f"<details><summary>其餘 {len(tail)} 列</summary>",
            "",
        ]
        lines += _rows_to_markdown(tail, columns, rename,
                                   suppressed_cells, na_marker)
        lines += ["", "</details>"]

    if notes:
        lines.append("")
        lines += notes
    if link:
        lines += ["", f"完整資料：[{link}.csv]({data_dir}/{link}.csv)"]
    return "\n".join(lines)


def render_definition_list(frame: pd.DataFrame, name: str,
                           data_dir: str = "data") -> str:
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
    lines += ["", f"完整資料：[{name}.csv]({data_dir}/{name}.csv)"]
    return "\n".join(lines)


def render_block(frame: pd.DataFrame, name: str, publish_csv: bool = True,
                 data_dir: str = "data", columns: list | None = None,
                 rename: dict | None = None,
                 suppressed_cells: set | None = None,
                 na_marker: str | None = None,
                 exempted_cells: set | None = None) -> str:
    if frame.empty:
        return "_（本次執行沒有資料）_"
    if name in DEFINITION_LIST_METRICS:
        return render_definition_list(frame, name, data_dir=data_dir)
    return render_table(frame, name if publish_csv else None, name,
                        data_dir=data_dir, columns=columns, rename=rename,
                        suppressed_cells=suppressed_cells, na_marker=na_marker,
                        exempted_cells=exempted_cells)


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

    from src import render_index
    render_index.check_bold_delimiters(text, "docs/RESULTS.md")

    changed = text != original
    if changed:
        RESULTS_PATH.write_text(text, encoding="utf-8", newline="\n")
    return {
        "changed": changed,
        "filled": filled,
        "missing_keys": missing_keys,
        "unknown_keys": unknown,
        "unmapped_metrics": [n for n in sorted(registry.REGISTRY) if n not in mapped],
    }


# 有手寫散文的檔案。**這份清單要跟著文件長。**
#
# 2026-08-27 踩過：INDEX.md 從「整份產生」改成標記式之後才第一次有手寫散文，
# 而這裡沒有跟著加，於是導覽裡一個過期的百分比完全沒被列出來。機制在、也正確
# 運作，只是涵蓋範圍不含實際出問題的那個檔案——與「測試驗了一個不會被執行的
# 路徑」是同一型的失效。
PROSE_SOURCES = (
    "docs/RESULTS.md",
    "docs/RESULTS_lite.md",
    "docs/OVERVIEW.md",
    "docs/PROGRESS.md",
    "docs/INDEX.md",
    "README.md",
)


def write_prose_numbers(run_id: str) -> Path:
    """列出所有手寫散文裡的數字。**不存在的檔案跳過，不報錯。**

    OVERVIEW.md 在清單裡但可能還沒建立——先登記後建立是對的順序：
    反過來的話，建了檔案而忘記登記，就又是一次「防護沒跟著文件長」。
    """
    sources = {}
    for name in PROSE_SOURCES:
        path = config.PROJECT_ROOT / name
        if path.exists():
            sources[name] = path.read_text(encoding="utf-8")
    target = config.RUNS_DIR / run_id / "prose_numbers.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(prose_number_report(sources), encoding="utf-8", newline="\n")
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
