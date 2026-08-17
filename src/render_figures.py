"""三張分位數帶的圖。

只畫表格會爆版的那幾張：分位數帶有 7–9 個數字欄，橫排放進 markdown 會
撐破版面，但畫成帶狀圖一眼就看得出形狀。其餘的表格本身就讀得動，不畫。

**圖上一律不出現中文。** matplotlib 在 Windows 上找不到中文字型會把每個字
畫成方框，而且不會報錯——圖看起來「產生成功」但沒有人看得懂。依賴系統字型
也讓同一份程式在不同機器上畫出不同的圖。中文說明留給 RESULTS.md 的散文。

**被抑制的資料不入圖。** 圖比表更容易讓人忽略抑制：表裡的空格會被注意到，
圖裡少一條線不會。所以畫之前先查 sidecar，命中就跳過並記警告。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 無視窗環境；也讓輸出不依賴後端差異

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from src import config  # noqa: E402

logger = logging.getLogger(__name__)

# 單色系。用色相區分不同的量會讓讀者以為顏色有語意，這三張圖沒有分類變數。
INK = "#1a1a1a"
MUTED = "#9a9a9a"
BAND = "#c9c9c9"
GRID = "#e6e6e6"

# 關掉 matplotlib 寫進 PNG 的 Software 標籤。留著的話每次升級 matplotlib
# 都會產生一個與內容無關的 git diff。
PNG_METADATA = {"Software": None}


def _style(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
        ax.spines[side].set_linewidth(0.8)
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK, labelsize=9, length=3, color=MUTED)


def _save(fig, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=150, bbox_inches="tight", facecolor="white",
                metadata=PNG_METADATA)
    plt.close(fig)
    return target


def _suppressed_groups(run_id: str, metric: str) -> set[str]:
    """該指標被抑制的分組值。沒有 sidecar 就是沒有抑制。"""
    path = config.RUNS_DIR / run_id / "metrics" / f"{metric}.suppressed.json"
    if not path.exists():
        return set()
    items = json.loads(path.read_text(encoding="utf-8"))
    return {str(item["分組值"]) for item in items}


def _guard(run_id: str, metric: str, labels) -> bool:
    """要畫的列有沒有被抑制？有就別畫。"""
    suppressed = _suppressed_groups(run_id, metric)
    hit = suppressed.intersection({str(v) for v in labels})
    if hit:
        logger.warning(
            "跳過 %s 的圖：要繪製的分組 %s 已被抑制。"
            "被抑制的值不入圖——圖比表更容易讓人忽略抑制",
            metric, sorted(hit),
        )
        return False
    return True


def _read(run_id: str, metric: str) -> pd.DataFrame | None:
    path = config.RUNS_DIR / run_id / "metrics" / f"{metric}.csv"
    if not path.exists():
        logger.warning("找不到 %s，跳過對應的圖", path)
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# 1 快取命中率 × turn 內位置
# ---------------------------------------------------------------------------
def cache_hit_by_position(run_id: str, out_dir: Path) -> Path | None:
    metric = "cache_hit_by_request_position"
    frame = _read(run_id, metric)
    if frame is None or not _guard(run_id, metric, frame["分組"]):
        return None

    labels = ["1st", "2nd", "3rd+"]
    frame = frame.set_index("分組").reindex(labels)

    fig, ax = plt.subplots(figsize=(7, 4.2))
    _style(ax)
    positions = range(len(labels))

    for x, label in zip(positions, labels):
        row = frame.loc[label]
        # p10–p90 細帶、p25–p75 粗帶、p50 一點。三層厚度取代圖例。
        ax.vlines(x, row["p10"], row["p90"], color=BAND, linewidth=9, zorder=2)
        ax.vlines(x, row["p25"], row["p75"], color=MUTED, linewidth=9, zorder=3)
        ax.plot([x], [row["p50"]], "o", color=INK, markersize=7, zorder=4)
        ax.annotate(f"p50 = {row['p50']:.3f}", (x, row["p50"]),
                    textcoords="offset points", xytext=(14, -3),
                    fontsize=9, color=INK)
        # n 標在繪圖區頂端，不放軸線下方——放下面會疊到兩行的刻度標籤。
        ax.annotate(f"n = {int(row['n']):,}", (x, 1.0),
                    textcoords="offset points", xytext=(0, 10),
                    ha="center", fontsize=8, color=MUTED)

    ax.set_xticks(list(positions))
    ax.set_xticklabels([f"{lab}\nrequest in turn" for lab in labels])
    ax.set_ylim(-0.03, 1.10)
    ax.set_ylabel("prompt cache hit rate", fontsize=10, color=INK)
    ax.set_title("Cache hit rate by request position within a turn",
                 fontsize=12, color=INK, loc="left", pad=30)
    ax.annotate("bands: p10-p90 (light) / p25-p75 (dark)",
                xy=(0, 1.06), xycoords="axes fraction",
                fontsize=8, color=MUTED)
    return _save(fig, out_dir / "cache_hit_by_position.png")


# ---------------------------------------------------------------------------
# 2 turn 展開深度
# ---------------------------------------------------------------------------
def turn_expansion_depth(run_id: str, out_dir: Path) -> Path | None:
    metric = "turn_expansion_depth"
    frame = _read(run_id, metric)
    if frame is None:
        return None
    frame = frame.set_index("統計量")

    stats = ["p10", "p25", "p50", "p75", "p90", "p99", "max"]
    series = {
        "All turns": [float(frame.loc[s, "全體"]) for s in stats],
        "Excluding compacted turns": [
            float(frame.loc[s, "排除有壓縮的 turn"]) for s in stats],
    }

    fig, ax = plt.subplots(figsize=(7.4, 4.4))
    _style(ax)
    x = range(len(stats))
    for (label, values), style in zip(series.items(),
                                      [("-", INK, 7), ("--", MUTED, 5)]):
        line, color, size = style
        ax.plot(x, values, line, color=color, linewidth=1.8,
                marker="o", markersize=size, zorder=3)
        # 直接標在線末，不開圖例框——圖例框會佔掉資料區又多一層對照成本。
        # 兩條線在 max 幾乎重疊，標籤錯開高度才不會互相蓋掉。
        lift = 14 if label.startswith("All") else -22
        ax.annotate(label, (len(stats) - 1, values[-1]),
                    textcoords="offset points", xytext=(-8, lift),
                    ha="right", fontsize=9, color=color)

    ax.set_yscale("log")
    ax.set_xticks(list(x))
    ax.set_xticklabels(stats)
    ax.set_ylabel("requests per turn (log scale)", fontsize=10, color=INK)
    ax.set_title("How many API requests one user action expands into",
                 fontsize=12, color=INK, loc="left", pad=14)
    ax.annotate(f"p50 = {series['All turns'][2]:.0f}   "
                f"max = {series['All turns'][-1]:.0f}   "
                f"n = {int(float(frame.loc['n_turns', '全體'])):,} turns",
                xy=(0, 1.02), xycoords="axes fraction",
                fontsize=8, color=MUTED)
    return _save(fig, out_dir / "turn_expansion_depth.png")


# ---------------------------------------------------------------------------
# 3 thread 內工具訊息佔比
# ---------------------------------------------------------------------------
def tool_message_ratio(run_id: str, out_dir: Path) -> Path | None:
    metric = "thread_tool_message_ratio"
    frame = _read(run_id, metric)
    if frame is None or not _guard(run_id, metric, frame["分組"]):
        return None
    row = frame.iloc[0]

    fig, ax = plt.subplots(figsize=(7.4, 3.4))
    _style(ax)
    y = 0

    ax.hlines(y, row["p10"], row["p90"], color=BAND, linewidth=22, zorder=2)
    ax.hlines(y, row["p25"], row["p75"], color=MUTED, linewidth=22, zorder=3)
    ax.plot([row["p50"]], [y], "|", color=INK, markersize=30,
            markeredgewidth=2.5, zorder=4)

    # p75 與 p90 只差 0.05，同高度會疊在一起，所以錯開兩層。
    offsets = {"p10": -30, "p25": -30, "p50": 22, "p75": -30, "p90": -52}
    for label, offset in offsets.items():
        value = float(row[label])
        ax.annotate(f"{label}\n{value:.2f}", (value, y),
                    textcoords="offset points", xytext=(0, offset),
                    ha="center", fontsize=8,
                    color=INK if label == "p50" else MUTED)

    ax.set_xlim(0, 1)
    ax.set_ylim(-0.75, 0.55)
    ax.set_yticks([])
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("share of thread messages that are tool traffic",
                  fontsize=10, color=INK)
    ax.set_title("Tool messages as a share of conversation history",
                 fontsize=12, color=INK, loc="left", pad=34)
    ax.annotate(f"n = {int(row['n']):,} threads   "
                f"zero-ratio threads = {float(row['zero_ratio_share']):.4f}",
                xy=(0, 1.10), xycoords="axes fraction",
                fontsize=8, color=MUTED)
    return _save(fig, out_dir / "tool_message_ratio.png")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run(run_id: str, out_dir: Path | None = None) -> list[str]:
    target = out_dir or (config.DOCS_DIR / "figures")
    produced: list[str] = []
    for builder in (cache_hit_by_position, turn_expansion_depth, tool_message_ratio):
        path = builder(run_id, target)
        if path is not None:
            produced.append(path.name)
            logger.info("圖 → %s", path)
    return produced
