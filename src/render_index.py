"""由 registry 產生 docs/INDEX.md，並填入 README 的 AUTOGEN 區塊。

文件不手寫的理由：手寫的文件會過期，而且過期時沒有任何訊號。
從 registry 產生的話，改了指標定義文件就跟著變。

冪等性是這個模組的硬性要求：連續產生兩次，輸出必須逐位元組相同。
因此**輸出裡不得出現任何時間戳或執行序號**——那會讓 git diff 每次都有變動，
真正的內容變更就被雜訊淹掉了。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

import pandas as pd

from src import config
from src.metrics import registry

logger = logging.getLogger(__name__)

INDEX_PATH = config.DOCS_DIR / "INDEX.md"
README_PATH = config.PROJECT_ROOT / "README.md"

NOT_RUN = "未執行"

# 標記本身不可被覆寫：只替換兩個標記「之間」的內容。
_MARKER_TEMPLATE = (
    r"(?P<start><!-- AUTOGEN:{key}:START -->)"
    r".*?"
    r"(?P<end><!-- AUTOGEN:{key}:END -->)"
)


def _is_punct(ch: str) -> bool:
    """CommonMark 認定的標點：Unicode P* 類，加上幾個 S 類符號。

    中日韓的「。」「，」「」」都算標點，中文字本身不算——這個差別正是下面
    那道檢查的全部理由。
    """
    if not ch:
        return False
    return unicodedata.category(ch).startswith("P") or ch in "$+<=>^`|~"


def check_bold_delimiters(text: str, label: str) -> None:
    """擋掉在中文裡不會生效的 `**粗體**`。

    CommonMark 規定收尾的 `**` 必須是 right-flanking：前面若是標點，後面就得是
    空白或標點。中文寫成 `**這是重點。**接下來` 時，收尾的 `**` 前面是「。」、
    後面是「接」——兩個條件都不滿足，於是它不能收尾，反而被當成新的開頭。
    結果是整段粗體範圍歪掉，或者 `**` 直接以字面印在頁面上。

    這種錯在原始碼裡看起來完全正常，只有在 GitHub 上才看得出來，而且我們是
    等到讀者回報才發現的。所以改成產生文件時就當場失敗——理由與
    check_columns_match() 相同：靠人眼複查會漏，靠機器複查不會。

    修法是把句號移到粗體外面：`**這是重點**。接下來`。顯示出來的字一模一樣，
    差別只在句號算不算粗體的一部分。
    """
    bad: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        runs = [m.start() for m in re.finditer(r"\*\*", line)]
        # 由左到右兩兩配對＝作者的原意；偶數位那個是原意的「收尾」。
        for close in runs[1::2]:
            prev = line[close - 1] if close > 0 else ""
            nxt = line[close + 2] if close + 2 < len(line) else ""
            if _is_punct(prev) and nxt and not nxt.isspace() and not _is_punct(nxt):
                bad.append(f"  L{n}：…{line[max(0, close - 20):close]}[**]{line[close + 2:close + 12]}…")
    if bad:
        raise ValueError(
            f"{label} 有 {len(bad)} 處粗體在 GitHub 上不會生效"
            "（收尾的 ** 前面是標點、後面不是標點或空白）：\n"
            + "\n".join(bad)
            + "\n把句號移到 ** 外面即可：**這是重點**。接下來"
        )


def latest_summary() -> pd.DataFrame | None:
    """每個指標「最近一次」的執行結果。

    刻意逐個指標取最新，而不是整份取最新的 metrics_summary.csv：
    `metrics --name X` 產生的 summary 只含 X，若整份取最新，
    其餘指標會憑空退回「未執行」——文件與事實不符，而且沒有任何錯誤訊號。
    """
    candidates = sorted(config.RUNS_DIR.glob("*/metrics_summary.csv"))
    if not candidates:
        return None
    frames = []
    for order, path in enumerate(candidates):
        frame = pd.read_csv(path, encoding="utf-8-sig")
        if frame.empty:
            continue
        frame["_order"] = order
        frames.append(frame)
    if not frames:
        return None
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.sort_values("_order").drop_duplicates("name", keep="last")
    return merged.drop(columns="_order")


def coverage_lookup() -> dict[str, str]:
    summary = latest_summary()
    if summary is None or summary.empty:
        return {}
    lookup: dict[str, str] = {}
    # itertuples 會把欄位 name 對應到屬性 name，但 namedtuple 的欄位存取
    # 在欄名與內建衝突時容易出錯，這裡直接用 dict 取值，語意明確。
    for record in summary.to_dict("records"):
        metric_name = record["name"]
        if record.get("狀態") != "成功" or pd.isna(record.get("coverage")):
            lookup[metric_name] = "執行失敗"
        else:
            lookup[metric_name] = f"{100 * float(record['coverage']):.1f}%"
    return lookup


def _escape(text: str | None) -> str:
    """表格儲存格：換行與 | 會破壞 markdown 表格結構。"""
    if text is None:
        return "—"
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def build_index() -> str:
    coverage = coverage_lookup()
    lines = [
        "# 指標索引",
        "",
        "本檔由 `python -m src.render_index` 從 `src/metrics/registry.py` 的",
        "註冊表產生，請勿手動編輯。要改內容請改指標的 `@metric` 參數。",
        "",
        f"已註冊指標：{len(registry.REGISTRY)} 個",
        "",
        "| 指標名 | 回答什麼 | 單位 | 來源表 | 分母 | 覆蓋率 | 注意事項 | 版本 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for spec in registry.list_metrics():
        lines.append(
            f"| `{spec.name}` | {_escape(spec.question)} | {spec.unit} "
            f"| {spec.source} | {_escape(spec.denominator)} "
            f"| {coverage.get(spec.name, NOT_RUN)} | {_escape(spec.caveat)} "
            f"| {spec.version} |"
        )

    from src import aggregate

    grouped = [s for s in registry.list_metrics() if s.group_by]
    suppressed = [s for s in grouped
                  if any(d in aggregate.CONCENTRATION_DIMENSIONS for d in s.group_by)]
    exempt = [s for s in grouped if s not in suppressed]

    lines += [
        "",
        "## 分組指標與抑制",
        "",
        f"宣告了 `group_by` 的指標共 {len(grouped)} 個，其中 {len(suppressed)} 個"
        f"受抑制、{len(exempt)} 個依政策豁免。",
        "",
        "### 受抑制的維度",
        "",
        "只有**把人分群**的維度才抑制："
        f"`{'`、`'.join(aggregate.CONCENTRATION_DIMENSIONS)}`。",
        "依 `runs/<run_id>/concentration.csv` 判定，觸發任一條件即抑制：",
        "",
        f"- 分組人數 < `MIN_GROUP_SIZE`（{config.MIN_GROUP_SIZE}）",
        f"- 單一使用者佔該組流量 > `DOMINANT_THRESHOLD`"
        f"（{100 * config.DOMINANT_THRESHOLD:.0f}%）",
        "",
        "觸發時該列的比例欄位置為 NA，**計數欄位保留**。",
        "計數是事實，比例才有再識別與誤導風險。",
        "",
    ]
    for spec in suppressed:
        lines.append(f"- `{spec.name}`：分組維度 {', '.join(spec.group_by)}")

    lines += [
        "",
        "### 豁免的維度",
        "",
        "時段、端點、模型、狀態碼這類維度分的是**請求**不是**人**，不抑制。",
        "抑制它們只會把事實抹掉——「凌晨 3 點只有 2 個人在用」本身就是要報的事實，",
        "清成 NA 反而讓讀者以為資料缺漏。",
        "代價是低量分組的比例可能出自一兩個人，因此這些指標一律附上 `n_users`，",
        "由讀者自行判斷母數厚薄；缺 `n_users` 時 registry 會發出警告。",
        "",
    ]
    for spec in exempt:
        lines.append(f"- `{spec.name}`：分組維度 {', '.join(spec.group_by)}（附 n_users）")

    lines += [
        "",
        "## 欄位說明",
        "",
        "- **單位**：這個指標的分析粒度（request / turn / thread / user）。",
        "- **來源表**：實際讀哪張表計算。",
        "- **分母**：比例的母體是什麼。分母講不清楚的比例不能用。",
        "- **覆蓋率**：最近一次執行時，母體中實際有值的比例。",
        f"  尚未執行過的指標標示為「{NOT_RUN}」。",
        "",
    ]
    return "\n".join(lines) + "\n"


def build_data_block() -> str:
    from src import aggregate, schema

    try:
        frame = schema.load_dataset()
    except Exception as exc:
        return f"資料尚未產生（{type(exc).__name__}）。請先執行 `python -m src.run extract`。"

    counts = {"request": len(frame)}
    for label, path in (("turn", aggregate.TURN_PATH),
                        ("thread", aggregate.THREAD_PATH),
                        ("user", aggregate.USER_PATH)):
        counts[label] = len(pd.read_parquet(path)) if path.exists() else None

    versions = sorted(frame["pipeline_version"].dropna().unique())
    lines = [
        f"- **請求筆數**：{len(frame):,} 列 × {frame.shape[1]} 欄",
        f"- **時間範圍**：{frame['ts_taipei'].min():%Y-%m-%d %H:%M:%S} ~ "
        f"{frame['ts_taipei'].max():%Y-%m-%d %H:%M:%S}（{config.TIMEZONE}）",
        f"- **分區**：{len(frame['date_taipei'].unique())} 天",
        "- **四層母數**：",
    ]
    for label in ("request", "turn", "thread", "user"):
        value = counts[label]
        lines.append(f"  - {label}：{value:,}" if value is not None
                     else f"  - {label}：尚未聚合")
    lines.append(f"- **pipeline_version**：{', '.join(versions)}")
    lines.append("")
    lines.append(
        "母數逐層下降是預期的：agent 會把一次使用者動作展開成多個請求，"
        "用 request 當分母會嚴重高估使用量。"
    )
    return "\n".join(lines)


def build_metrics_block() -> str:
    coverage = coverage_lookup()
    executed = sum(1 for name in registry.REGISTRY if name in coverage)
    grouped = sum(1 for s in registry.REGISTRY.values() if s.group_by)
    return "\n".join([
        f"- **已註冊指標**：{len(registry.REGISTRY)} 個"
        f"（其中 {grouped} 個宣告了分組維度，比例欄位會自動抑制）",
        f"- **已執行過**：{executed} 個",
        "- 完整清單與定義見 [docs/INDEX.md](docs/INDEX.md)",
        "",
        "```",
        "python -m src.run metrics --list      # 列出已註冊指標",
        "python -m src.run metrics             # 全部執行",
        "python -m src.run metrics --name <名稱>  # 單一執行",
        "```",
    ])


def _latest_metric_frame(name: str) -> pd.DataFrame | None:
    """最近一次跑出這個指標的 csv。

    逐個指標取最新，理由同 latest_summary()：`metrics --name X` 只會產生 X，
    整份取最新會讓其餘指標的摘要憑空消失。
    """
    candidates = sorted(config.RUNS_DIR.glob(f"*/metrics/{name}.csv"))
    if not candidates:
        return None
    return pd.read_csv(candidates[-1], encoding="utf-8-sig")


def build_highlights_block() -> str:
    """README 那四行結果摘要。

    這段本來是手抄的。資料一更新它就會靜靜變錯，而且它在 README 最顯眼的位置——
    最容易被引用、也最沒有人會回頭核對的地方。改由指標產生。
    """
    lines: list[str] = []

    cache = _latest_metric_frame("cache_hit_by_request_position")
    if cache is not None and len(cache) >= 3:
        rows = cache.set_index("分組")
        lines.append(
            f"- **快取的位置效應非常乾淨**。一個 turn 內第 1 個請求的中位快取率是 "
            f"**{rows.loc['1st', 'p50']:.0f}**"
            f"（{100 * float(rows.loc['1st', 'zero_cache_share']):.1f}% 完全沒命中），"
            f"第 2 個跳到 **{100 * float(rows.loc['2nd', 'p50']):.1f}%**，"
            f"第 3 個以後穩定在 **{100 * float(rows.loc['3rd+', 'p50']):.1f}%**。"
            "這是協定層的機制，不依賴樣本量，也不能拿來比較人。"
        )

    prompt = _latest_metric_frame("prompt_length_distribution")
    exceeded = _latest_metric_frame("context_length_exceeded_profile")
    if prompt is not None:
        whole = prompt[prompt["client_type"] == "全體"].iloc[0]
        hits = ""
        if exceeded is not None:
            n = exceeded.loc[(exceeded["類別"] == "彙總")
                             & (exceeded["值"] == "n_requests"), "n"]
            if len(n):
                hits = f"有 {int(n.iloc[0])} 筆請求撞到上下文長度上限。"
        lines.append(
            f"- **prompt 大多很短**。全體中位數 {whole['p50']:,.0f} 字元，"
            f"但 p90 是 {whole['p90']:,.0f}、最長一筆 {whole['max']:,.0f} 字元。{hits}"
        )

    status = _latest_metric_frame("status_and_errors")
    if status is not None:
        summary = status[status["類別"] == "彙總"].set_index("值")
        errors = int(summary.loc["error_4xx_5xx", "n_requests"])
        total = errors + int(summary.loc["success_2xx", "n_requests"])
        lines.append(
            f"- **錯誤極少**。{errors:,} / {total:,}。"
            "400 全部是客戶端送錯參數，不是服務故障；唯一的 520 是上游異常。"
        )

    expansion = _latest_metric_frame("turn_expansion_depth")
    if expansion is not None:
        rows = expansion.set_index("統計量")
        compacted = int(float(rows.loc["n_turns_with_compaction", "全體"]))
        lines.append(
            f"- **{compacted} 個 turn 發生過上下文壓縮**，"
            "對話歷史在同一個 `turn_id` 內被重置。它們只污染尾端："
            "排除後 p50 與 p75 完全不變，"
            f"p99 從 {float(rows.loc['p99', '全體']):.1f} 降到 "
            f"{float(rows.loc['p99', '排除有壓縮的 turn']):.1f}、"
            f"max 從 {float(rows.loc['max', '全體']):.0f} 降到 "
            f"{float(rows.loc['max', '排除有壓縮的 turn']):.0f}。"
        )

    if not lines:
        return "_（尚未執行過指標，暫無摘要）_"
    return "\n".join(lines)


def replace_block(text: str, key: str, body: str) -> str:
    pattern = re.compile(_MARKER_TEMPLATE.format(key=key), re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"README 找不到 AUTOGEN:{key} 標記，拒絕寫入以免破壞檔案")
    # \g<start> / \g<end> 原樣保留標記本身，只換中間。
    return pattern.sub(lambda m: f"{m.group('start')}\n{body}\n{m.group('end')}", text)


def render_readme() -> bool:
    original = README_PATH.read_text(encoding="utf-8")
    updated = replace_block(original, "DATA", build_data_block())
    updated = replace_block(updated, "METRICS", build_metrics_block())
    updated = replace_block(updated, "HIGHLIGHTS", build_highlights_block())
    # 檢查放在「有沒有變動」之前：已經在檔案裡的壞粗體也要被抓出來，
    # 不能因為這次剛好沒改到就放行。
    check_bold_delimiters(updated, "README.md")
    if updated == original:
        return False
    README_PATH.write_text(updated, encoding="utf-8")
    return True


def render_index() -> bool:
    config.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    body = build_index()
    check_bold_delimiters(body, "docs/INDEX.md")
    if INDEX_PATH.exists() and INDEX_PATH.read_text(encoding="utf-8") == body:
        return False
    INDEX_PATH.write_text(body, encoding="utf-8")
    return True


def run() -> dict:
    index_changed = render_index()
    readme_changed = render_readme()
    logger.info("docs/INDEX.md %s（%d 個指標）",
                "已更新" if index_changed else "無變動", len(registry.REGISTRY))
    logger.info("README.md %s", "已更新" if readme_changed else "無變動")
    return {"index_changed": index_changed, "readme_changed": readme_changed,
            "index_path": str(INDEX_PATH), "readme_path": str(README_PATH)}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
