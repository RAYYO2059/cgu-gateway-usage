"""掃相異 prompt 的結構標記，輸出旗標與雜湊。**明文不落地。**

判定字串放 ref/prefilter_markers.csv 而不是寫死在這裡，理由與
college_mapping.csv、pricing_table.csv 相同：它是會隨上游變動的對照資料，
不是邏輯。上游工具改版就多一條標記，那是改表不是改程式。

用**結構標記**判定而不是 sha256 清單：外框固定、內容每次不同，雜湊清單
在內容一變的當下就失效，而且失效沒有徵兆——漏掉的請求不會報錯，只會安靜
地落進「需要人工判斷」那一堆。見 DESIGN_NOTES〈偵測「同一支程式」要用框架〉。

每個相異 prompt 只讀一次代表檔：31,011 個相異內容對應 106,993 筆請求，
逐筆讀會做三倍半的白工。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from src import config, extract_lite

logger = logging.getLogger(__name__)

MARKERS_PATH = config.DATA_AGG_LITE / "markers_lite.parquet"
MANIFEST_PATH = config.MANIFEST_LITE / "markers_progress.parquet"
MARKER_TABLE = config.REF_DIR / "prefilter_markers.csv"

# 與 prefilter 共用：兩邊都要用同一個下限，否則母數對不起來。
MIN_LEN = 20
CHUNK_SIZE = 5000

# 輸出欄位裡出現這些就是明文外洩。與 schema_lite.check_columns_match() 同性質：
# 「不落地」這件事要由程式擋住，不是靠寫程式的人記得。
FORBIDDEN_COLUMNS = ("prompt_text", "prompt", "text", "content", "message")


def load_marker_table() -> pd.DataFrame:
    """讀判定字串表。缺表就 raise——空表繼續跑會產出「零命中」的假結果。"""
    if not MARKER_TABLE.exists():
        raise FileNotFoundError(
            f"找不到 {MARKER_TABLE}。判定字串是對照資料不是預設值，"
            "缺表時不可用空清單繼續跑：那會讓四條規則全部零命中，"
            "而分類母數看起來仍然是個合理的數字。"
        )
    table = pd.read_csv(MARKER_TABLE, encoding="utf-8-sig", dtype=str)
    missing = {"規則", "標記名", "判定字串", "是否為排除項"} - set(table.columns)
    if missing:
        raise ValueError(f"{MARKER_TABLE} 缺欄位 {sorted(missing)}")
    return table


def marker_columns(table: pd.DataFrame) -> list[str]:
    """旗標欄名。規則編號寫進欄名，prefilter 才不必再查表對應。"""
    names = []
    for row in table.itertuples():
        prefix = "exclude" if str(row.是否為排除項).strip() == "是" else f"r{row.規則}"
        names.append(f"{prefix}_{row.標記名}")
    return names


def check_columns(frame: pd.DataFrame) -> None:
    """輸出欄位不得含明文。**這條是硬斷言，不是警告。**

    明文一旦寫進 parquet 就會跟著 data/ 被備份、被複製，再也收不回來。
    """
    bad = [c for c in frame.columns if c.lower() in FORBIDDEN_COLUMNS]
    if bad:
        raise AssertionError(
            f"markers 輸出出現明文欄位 {bad}。本階段只輸出雜湊與旗標；"
            "要看內容請直接讀原始 JSON，不要讓它落地。"
        )


def _representatives() -> pd.DataFrame:
    """每個相異 prompt 取一筆代表（最早的那筆），帶著它的來源路徑。"""
    frame = extract_lite.load_dataset()
    sub = frame[frame["prompt_text_sha256"].notna()
                & (frame["prompt_text_len"] >= MIN_LEN)]
    return (sub.sort_values("ts_utc")
            .groupby("prompt_text_sha256", as_index=False)
            .first()[["prompt_text_sha256", "source_path"]])


def _load_progress() -> pd.DataFrame:
    if MANIFEST_PATH.exists():
        try:
            return pd.read_parquet(MANIFEST_PATH)
        except Exception as exc:
            logger.warning("markers 進度檔讀取失敗（視為從頭開始）：%s: %s",
                           type(exc).__name__, exc)
    return pd.DataFrame()


def run(chunk_size: int = CHUNK_SIZE, max_files: int | None = None) -> int:
    """掃描代表檔並累積旗標。每塊寫一次檔，中斷最多損失一塊。"""
    table = load_marker_table()
    needles = list(table["判定字串"])
    columns = marker_columns(table)

    rep = _representatives()
    done = _load_progress()
    seen = set(done["prompt_text_sha256"]) if not done.empty else set()
    pending = rep[~rep["prompt_text_sha256"].isin(seen)]
    logger.info("相異 prompt %s，已掃 %s，待掃 %s",
                f"{len(rep):,}", f"{len(seen):,}", f"{len(pending):,}")
    if max_files is not None:
        pending = pending.head(max_files)

    root = config.lite_raw_dir()
    config.DATA_AGG_LITE.mkdir(parents=True, exist_ok=True)
    config.MANIFEST_LITE.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    n_unreadable = 0
    for start in range(0, len(pending), chunk_size):
        block = pending.iloc[start:start + chunk_size]
        for row in block.itertuples():
            path = root / row.source_path
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                text = (record.get("request") or {}).get("prompt_text") or ""
            except Exception:
                # 讀不到就是所有旗標為 False，但要數出來：靜默的話，
                # 「沒有命中」與「沒讀到」會變成同一件事。
                text, n_unreadable = "", n_unreadable + 1
            flags = {"prompt_text_sha256": row.prompt_text_sha256}
            flags.update({c: (n in text) for c, n in zip(columns, needles)})
            rows.append(flags)
            del text  # 明文只在這個迴圈裡存在
        done = pd.concat([done, pd.DataFrame(rows)], ignore_index=True)
        rows = []
        check_columns(done)
        done.to_parquet(MANIFEST_PATH, index=False)
        logger.info("第 %d/%d 塊：累計 %s 個相異 prompt",
                    start // chunk_size + 1,
                    (len(pending) + chunk_size - 1) // chunk_size or 1,
                    f"{len(done):,}")

    if n_unreadable:
        logger.warning("有 %d 個代表檔讀不到，其旗標一律 False", n_unreadable)

    check_columns(done)
    done.to_parquet(MARKERS_PATH, index=False)
    logger.info("markers %s 列 → %s", f"{len(done):,}", MARKERS_PATH)
    for column in columns:
        logger.info("  %-28s %6s", column, f"{int(done[column].sum()):,}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="掃相異 prompt 的結構標記（讀原始 JSON，約 25 分鐘）")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE,
                        help="每塊幾個相異 prompt（只影響存檔頻率）")
    parser.add_argument("--max-files", type=int, default=None,
                        help="本次最多掃幾個，用於分段執行")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    return run(chunk_size=args.chunk_size, max_files=args.max_files)


if __name__ == "__main__":
    raise SystemExit(main())
