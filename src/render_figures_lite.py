"""lite 這條線的四張圖。

規範沿用 ``render_figures``（clean 那三張），三條都不可以放寬：

**圖上一律不出現中文。** matplotlib 在 Windows 上找不到中文字型會把每個字畫成
方框，而且不會報錯——圖看起來「產生成功」但沒有人看得懂。中文說明留給
``docs/RESULTS_lite.md`` 的對照表。

**PNG 的 Software metadata 關掉。** 留著的話每次升級 matplotlib 都會產生一個
與內容無關的 git diff。加上 Agg 後端，同樣的輸入就會產生位元組相同的 PNG。

**被抑制的值不入圖。** 圖比表更容易讓人忽略抑制：表裡的空格會被注意到，圖裡
少一條線不會。這一點在圖 4 特別關鍵——見該函式的說明。

輸出到 ``docs/figures_lite/``，與 clean 的 ``docs/figures/`` 分開：兩批資料的
期間與母體不同，混在一個目錄裡，讀者拿到 png 無從判斷它屬於哪一批。
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 無視窗環境；也讓輸出不依賴後端差異

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from src import config  # noqa: E402
from src.render_figures import GRID, INK, MUTED, PNG_METADATA, _style  # noqa: E402

logger = logging.getLogger(__name__)

# 四段成本的固定顏色。順序與金額語意一致（未快取最貴 → 快取讀最便宜），
# 用灰階深淺而不是色相：色相會讓讀者以為四段有類別語意，它們其實是同一筆錢
# 的四個部分。
SEGMENT_COLORS = {
    "input_cost": "#2b2b2b",
    "cached_cost": "#7a7a7a",
    "cache_write_cost": "#a8a8a8",
    "output_cost": "#d0d0d0",
}
SEGMENT_LABELS = {
    "input_cost": "Uncached prompt",
    "cached_cost": "Cache read",
    "cache_write_cost": "Cache write",
    "output_cost": "Output",
}

# 圖上用英文，中文對照表放 RESULTS_lite.md。這份對照是**單一來源**——
# 對照表由 build_glossary() 從這裡產生，不手寫，否則兩邊遲早對不上。
UNIT_EN = {
    "教職員": "Faculty & staff",
    "服務憑證": "Service credentials",
    "工學院": "College of Engineering",
    "管理學院": "College of Management",
    "智慧運算學院": "College of Intelligent Computing",
    "醫學院": "College of Medicine",
    "(未對應)": "(unmapped)",
}

# 圖上放不下全名，散點圖用短標籤。
UNIT_SHORT = {
    "教職員": "Faculty",
    "服務憑證": "Service cred.",
    "工學院": "Engineering",
    "管理學院": "Management",
    "智慧運算學院": "Intel. Computing",
    "醫學院": "Medicine",
    "(未對應)": "(unmapped)",
}

# matplotlib 會把成對的 $ 當成 mathtext，於是 "$0.0037  $0.0071" 被吃成
# "0.00370.0071"——**不會報錯**，只是字少了。金額前的錢字號一律用這個常數，
# 不要直接寫 "$"。單獨一個 $ 剛好沒事，所以這種錯會等到某張圖出現第二個
# 金額時才突然浮出來。
USD = "\\$"

TOP_N_MODELS = 8


def _read(run_id: str, metric: str) -> pd.DataFrame | None:
    path = config.RUNS_DIR / run_id / "metrics_lite" / f"{metric}.csv"
    if not path.exists():
        logger.warning("找不到 %s，跳過對應的圖", path)
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


def _save(fig, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=150, bbox_inches="tight", facecolor="white",
                metadata=PNG_METADATA)
    plt.close(fig)
    return target


# ---------------------------------------------------------------------------
# 逐日兩張圖共用的 x 軸
# ---------------------------------------------------------------------------
def _daily_axis(frame: pd.DataFrame):
    """把有資料的日子放回**完整的曆日軸**上。

    直接用 24 個有資料的日子當 x 軸，中斷的那兩天會被擠掉，圖上看不出有缺口
    ——那正是最需要看到的東西。所以先建完整日期範圍，缺的日子留 NaN，
    matplotlib 會自動斷線。
    """
    dates = pd.to_datetime(frame["date_taipei"])
    full = pd.date_range(dates.min(), dates.max(), freq="D")
    indexed = frame.copy()
    indexed["_d"] = dates
    indexed = indexed.set_index("_d").reindex(full)
    missing = [d for d in full if d not in set(dates)]
    return full, indexed, missing


def _mark_gaps_and_partial(ax, full, indexed, missing, label_y):
    """標出服務中斷與不完整的最後一天。

    兩者都不是使用行為，但長得像：中斷若連線會看起來像平滑下降再回升，
    最後一天若不標會看起來像斷崖式下跌。
    """
    for day in missing:
        ax.axvspan(day - pd.Timedelta(hours=12), day + pd.Timedelta(hours=12),
                   color="#f0f0f0", zorder=0)
    if missing:
        mid = missing[len(missing) // 2]
        ax.annotate("service outage\n(73 h, no requests)", xy=(mid, label_y),
                    ha="center", va="top", fontsize=8, color=MUTED)

    last = full[-1]
    ax.axvline(last, color=MUTED, linewidth=0.8, linestyle=":", zorder=1)
    ax.annotate("last day partial\n(export cut-off)", xy=(last, label_y),
                textcoords="offset points", xytext=(-6, 0),
                ha="right", va="top", fontsize=8, color=MUTED)


def _daily_ticks(ax, full):
    """兩張圖用同一組刻度，才對得齊。"""
    ticks = [d for d in full if d.day % 5 == 0 or d == full[0] or d == full[-1]]
    ax.set_xticks(ticks)
    ax.set_xticklabels([d.strftime("%m-%d") for d in ticks], fontsize=8)
    ax.set_xlim(full[0] - pd.Timedelta(hours=18),
                full[-1] + pd.Timedelta(hours=18))


# ---------------------------------------------------------------------------
# 1 逐日請求數
# ---------------------------------------------------------------------------
def daily_requests(run_id: str, out_dir: Path) -> Path | None:
    frame = _read(run_id, "cost_by_day_lite")
    if frame is None:
        return None
    full, indexed, missing = _daily_axis(frame)

    fig, ax = plt.subplots(figsize=(9, 3.4))
    _style(ax)
    values = indexed["n_requests"]
    top = float(values.max())
    _mark_gaps_and_partial(ax, full, indexed, missing, top * 0.97)

    ax.plot(full, values, color=INK, linewidth=1.6, zorder=4)
    ax.plot(full, values, "o", color=INK, markersize=3.5, zorder=5)
    ax.set_ylim(0, top * 1.12)
    ax.set_ylabel("requests per day", fontsize=10, color=INK)
    _daily_ticks(ax, full)
    ax.set_title("Daily requests (lite)", fontsize=12, color=INK,
                 loc="left", pad=10)
    return _save(fig, out_dir / "daily_requests.png")


# ---------------------------------------------------------------------------
# 2 逐日每筆成本
# ---------------------------------------------------------------------------
def daily_cost_per_request(run_id: str, out_dir: Path) -> Path | None:
    """**刻意與圖 1 分開，不畫雙軸。**

    08-04／08-05 各兩萬多筆但每筆只有 $0.0037／$0.0071。疊在同一組軸上時，
    請求數畫出兩根尖峰而成本線完全平坦——讀者只會看到「有兩天特別忙」。
    分成兩張、共用 x 軸，「請求最高的兩天，每筆成本最低」這件事自己會浮出來。
    """
    frame = _read(run_id, "cost_by_day_lite")
    if frame is None:
        return None
    full, indexed, missing = _daily_axis(frame)

    fig, ax = plt.subplots(figsize=(9, 3.4))
    _style(ax)
    values = indexed["cost_per_request"]
    top = float(values.max())
    _mark_gaps_and_partial(ax, full, indexed, missing, top * 0.97)

    ax.plot(full, values, color=INK, linewidth=1.6, zorder=4)
    ax.plot(full, values, "o", color=INK, markersize=3.5, zorder=5)

    # 把請求數最高的兩天標出來——那兩天正是每筆成本最低的兩天。
    #
    # 兩天相鄰，各標一次會讓兩段文字疊在一起。改成把兩點畫成空心圈、
    # 文字只出現一次並列出兩個值：疊字比少一個標註更難讀。
    busiest = sorted(indexed["n_requests"].nlargest(2).index)
    peaks = [float(indexed.loc[d, "cost_per_request"]) for d in busiest]
    counts = [int(indexed.loc[d, "n_requests"]) for d in busiest]
    ax.plot(busiest, peaks, "o", markersize=8, markerfacecolor="white",
            color=INK, zorder=6)
    label = ("2 busiest days (" + " / ".join(f"{c:,}" for c in counts)
             + " requests)\n" + "  ".join(f"{USD}{v:.4f}" for v in peaks)
             + " per request")
    mid = busiest[0] + (busiest[-1] - busiest[0]) / 2
    # 文字放左上的空白區、用引線指回資料點：貼著點放會壓在折線上。
    ax.annotate(label, xy=(mid, max(peaks)),
                xytext=(0.03, 0.90), textcoords="axes fraction",
                ha="left", va="top", fontsize=8, color=INK,
                arrowprops=dict(arrowstyle="-", color=MUTED, linewidth=0.8))

    ax.set_ylim(0, top * 1.18)
    ax.set_ylabel("cost per request (USD)", fontsize=10, color=INK)
    _daily_ticks(ax, full)
    ax.set_title("Daily cost per request (lite)", fontsize=12, color=INK,
                 loc="left", pad=10)
    return _save(fig, out_dir / "daily_cost_per_request.png")


# ---------------------------------------------------------------------------
# 3 成本結構（per model_family 堆疊）
# ---------------------------------------------------------------------------
def cost_structure_by_model(run_id: str, out_dir: Path) -> Path | None:
    """四段的整體佔比只有四個數字，畫圓餅資訊量不足。

    改成前 N 名模型族的堆疊長條，一張圖同時給三件事：哪個模型花最多、
    四段的整體形狀、以及**各模型的成本結構不一樣**（有些模型完全沒有
    快取寫入那一段）。
    """
    frame = _read(run_id, "cost_by_model_family_lite")
    if frame is None:
        return None
    frame = frame.sort_values("cost_usd", ascending=False).head(TOP_N_MODELS)
    frame = frame.iloc[::-1]  # 由下往上遞增，最貴的在最上面

    fig, ax = plt.subplots(figsize=(9, 4.4))
    _style(ax)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.grid(False, axis="y")

    positions = range(len(frame))
    left = [0.0] * len(frame)
    for column, colour in SEGMENT_COLORS.items():
        values = frame[column].fillna(0).tolist()
        ax.barh(list(positions), values, left=left, height=0.68,
                color=colour, edgecolor="white", linewidth=0.6, zorder=3,
                label=SEGMENT_LABELS[column])
        left = [a + b for a, b in zip(left, values)]

    for y, (total, share) in enumerate(zip(frame["cost_usd"],
                                           frame["cost_share"])):
        ax.annotate(f"{USD}{total:,.2f}  ({100 * float(share):.1f}%)",
                    xy=(total, y), textcoords="offset points", xytext=(6, 0),
                    va="center", fontsize=8, color=INK)

    ax.set_yticks(list(positions))
    ax.set_yticklabels(frame["model_family"], fontsize=9)
    ax.set_xlabel("cost (USD)", fontsize=10, color=INK)
    ax.set_xlim(0, float(frame["cost_usd"].max()) * 1.28)
    ax.legend(loc="lower right", frameon=False, fontsize=8, ncols=2)
    ax.set_title(f"Cost structure by model family (top {TOP_N_MODELS})",
                 fontsize=12, color=INK, loc="left", pad=10)
    return _save(fig, out_dir / "cost_structure_by_model.png")


# ---------------------------------------------------------------------------
# 4 流量與成本的錯位
# ---------------------------------------------------------------------------
def traffic_cost_mismatch(run_id: str, out_dir: Path) -> Path | None:
    """**用計數不用佔比，理由是抑制。**

    服務憑證那一列的 ``request_share`` 與 ``cost_share`` 都被抑制成 NA
    （單人佔 68.9% > 30%），而它正是錯位最大的一列。畫佔比的話，要嘛把被
    抑制的值畫上去（違反規範），要嘛把最重要的那一點拿掉（圖就沒有意義了）。

    改用 ``n_requests`` 與 ``cost_usd``——**計數不受抑制，計數是事實**。
    對數軸上，「成本與流量成正比」是一條直線；偏離那條線的距離就是錯位，
    而且七個單位全部畫得出來。
    """
    frame = _read(run_id, "cost_by_unit_lite")
    if frame is None:
        return None

    fig, ax = plt.subplots(figsize=(8.2, 5.4))
    _style(ax)
    ax.set_xscale("log")
    ax.set_yscale("log")

    total_requests = float(frame["n_requests"].sum())
    total_cost = float(frame["cost_usd"].sum())
    average = total_cost / total_requests

    frame = frame.sort_values("n_requests", ascending=False).reset_index(drop=True)
    frame["_ratio"] = frame["cost_per_request"].astype(float) / average

    # 「錯位只發生在前兩名」這件事，門檻式的實心／空心表達不出來——用固定的
    # 20% 門檻會讓七點中的五點都變實心，反而看不出誰是例外。
    #
    # 改成資料驅動的帶：**去掉最高與最低兩個極端，其餘單位張出的區間**。
    # 落在帶內的畫空心，帶外的（就是那兩個極端）畫實心。規則寫在圖上。
    ordered = frame["_ratio"].sort_values()
    middle = ordered.iloc[1:-1]
    low, high = float(middle.min()), float(middle.max())

    xs = frame["n_requests"].astype(float)
    line_x = [xs.min() * 0.5, xs.max() * 2.0]
    ax.fill_between(line_x, [low * average * x for x in line_x],
                    [high * average * x for x in line_x],
                    color="#ededed", zorder=1)
    ax.plot(line_x, [average * x for x in line_x], color=MUTED,
            linewidth=1.0, linestyle="--", zorder=2)
    ax.annotate(f"proportional ({USD}{average:.4f}/request)",
                xy=(line_x[1], average * line_x[1]),
                textcoords="offset points", xytext=(-6, 6),
                ha="right", fontsize=8, color=MUTED)

    for position, row in frame.iterrows():
        ratio = float(row["_ratio"])
        inside = low <= ratio <= high
        ax.plot([row["n_requests"]], [row["cost_usd"]], "o",
                markersize=9, zorder=4, color=INK,
                markerfacecolor="white" if inside else INK)
        # 相鄰的點會讓兩段標籤疊在一起。上下微調不夠——兩個點在對數軸上
        # 只差 1.6 倍時，垂直錯開的距離小於一行字高。改成**左右交替**：
        # 已依請求數排序，所以相鄰的點必定分到相反的兩側，
        # 分隔距離是標籤寬度而不是行高。順序固定，輸出仍可重現。
        right_side = position % 2 == 0
        ax.annotate(f"{UNIT_SHORT.get(row['unit'], row['unit'])}  ×{ratio:.2f}",
                    xy=(row["n_requests"], row["cost_usd"]),
                    textcoords="offset points",
                    xytext=(13 if right_side else -13, -3),
                    ha="left" if right_side else "right",
                    fontsize=8.5, color=INK)

    ax.set_xlim(line_x[0], line_x[1] * 1.6)
    ax.set_xlabel("requests (log scale)", fontsize=10, color=INK)
    ax.set_ylabel("cost, USD (log scale)", fontsize=10, color=INK)
    ax.set_title("Traffic vs cost by unit", fontsize=12, color=INK,
                 loc="left", pad=46)
    # 單位數由 middle 算，不寫死。系代碼對照表補齊後「(未對應)」那一列消失，
    # 七個單位變六個、中間五個變四個——寫死的話圖上會留下一句錯的說明，
    # 而圖是給人看的，沒有任何測試會發現。
    ax.annotate(
        f"band = the {len(middle)} units between the extremes "
        f"(x{low:.2f} to x{high:.2f}); filled = outside it. "
        f"xN = cost per request vs overall average.",
        xy=(0, 1.085), xycoords="axes fraction", fontsize=8, color=MUTED)
    ax.annotate("counts, not shares: the service-credential row's shares are "
                "suppressed, and counts are facts.",
                xy=(0, 1.035), xycoords="axes fraction", fontsize=8, color=MUTED)
    return _save(fig, out_dir / "traffic_cost_mismatch.png")


# ---------------------------------------------------------------------------
def build_glossary(run_id: str) -> str:
    """中英對照表。**由圖上實際用到的對照字典產生，不手寫。**

    手寫的話，改了圖上的標籤而忘了改表，讀者會對不上——而那種錯不會有任何
    訊號，表看起來仍然完整。
    """
    lines = ["| 圖上的英文 | 中文 | 說明 |", "| --- | --- | --- |"]
    for zh, en in UNIT_EN.items():
        short = UNIT_SHORT[zh]
        note = "圖 4 用短標籤" if short != en else ""
        label = f"`{en}`" + (f"／`{short}`" if short != en else "")
        lines.append(f"| {label} | {zh} | {note} |")
    for column, en in SEGMENT_LABELS.items():
        lines.append(f"| `{en}` | {_SEGMENT_ZH[column]} | 成本四段之一，"
                     f"對應 csv 欄位 `{column}` |")

    frame = _read(run_id, "cost_by_model_family_lite")
    if frame is not None:
        families = (frame.sort_values("cost_usd", ascending=False)
                    .head(TOP_N_MODELS)["model_family"].tolist())
        lines.append(f"| `{'`、`'.join(families)}` | 模型族名稱 | "
                     "原文即為英文，已剝除 `-YYYY-MM-DD` 版本後綴 |")

    lines += [
        "",
        "圖上不出現中文是刻意的：matplotlib 在找不到中文字型的機器上會把每個字",
        "畫成方框而且不報錯——圖看起來產生成功，但沒有人看得懂。",
    ]
    return "\n".join(lines)


_SEGMENT_ZH = {
    "input_cost": "未快取 prompt",
    "cached_cost": "快取讀",
    "cache_write_cost": "快取寫",
    "output_cost": "輸出",
}


def run(run_id: str, out_dir: Path | None = None) -> list[str]:
    target = out_dir or (config.DOCS_DIR / "figures_lite")
    produced: list[str] = []
    for builder in (daily_requests, daily_cost_per_request,
                    cost_structure_by_model, traffic_cost_mismatch):
        path = builder(run_id, target)
        if path is not None:
            produced.append(path.name)
            logger.info("圖 → %s", path)
    return produced
