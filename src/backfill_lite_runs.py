"""一次性工具：回填 2026-08-26 那 7 次 lite 抽取的 run_manifest.json。

那 7 次執行發生在 extract_lite 接上 RunManifest 之前，所以沒有留下自我描述
檔，但它們寫進 data/01_request_lite 的 120,520 列現在還在用。這支腳本把那段
血統補回去，讓「這批 parquet 怎麼來的」查得到。

**它只適用於那一批，跑完就不該再跑。** 留在 repo 裡是為了讓回填本身可稽核
——回填出來的 manifest 落在 runs/ 底下、不進版控，所以能被檢視的只有這支
腳本記錄的重建方法。

重建得出來的照重建，重建不出來的一律標「回填，未知」——不猜。
資料來源三個，互相印證：
  - parquet 檔名 → run_id、chunk 序號、涉及分區、每檔列數（讀 footer）
  - _manifest_lite 的 ingested_at → 每次執行的起始 UTC 時間、檔數、版本
  - 檔案 mtime → 每次執行的實際寫入時間範圍（推得結束時間）
"""
from __future__ import annotations

import collections
import datetime
import json
import pathlib
import re
import pandas as pd
import pyarrow.parquet as pq

from src import config

UNKNOWN = "回填，未知"
BACKFILLED_AT = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

# 當時的原始資料位置。搬進 data/00_raw_lite 是後來的事，據實填當時的值。
RAW_ROOT_THEN = r"C:\Users\isrea\Downloads\lite_v2_raw"

# 每次執行的用途。這是我確知的事實（來自當時的操作），不是推測。
PURPOSE = {
    1: "驗證分塊寫入：限定前 2,500 個來源檔、chunk_size=1000",
    2: "驗證中斷後接續：限定前 3,500 個來源檔，應跳過已處理的 2,500",
    3: "全量第 1 批：--max-files 45000 --chunk-size 5000，逾時中止於第 7/9 塊",
    4: "全量第 2 批：--max-files 40000 --chunk-size 5000，逾時中止於第 6/8 塊",
    5: "全量第 3 批：--max-files 40000 --chunk-size 5000，逾時中止於第 7/8 塊",
    6: "全量第 4 批：--chunk-size 5000，逾時中止於第 5/6 塊",
    7: "全量第 5 批：--chunk-size 3500，跑完，尚未處理歸零",
}
# 逾時中止的那幾批，程式本身沒有正常結束。
KILLED = {3, 4, 5, 6}

def main() -> int:
    pat = re.compile(r"^part-(?P<run>.+?)-c(?P<chunk>\d{4})-(?P<i>\d+)\.parquet$")

    root = config.DATA_REQUEST_LITE
    by_run: dict[str, list[tuple[pathlib.Path, int]]] = collections.defaultdict(list)
    for p in sorted(root.rglob("*.parquet")):
        m = pat.match(p.name)
        if not m:
            print(f"  ★ 檔名不符樣式，略過：{p.name}")
            continue
        by_run[m.group("run")].append((p, int(m.group("chunk"))))

    ingest = (pd.read_parquet(config.MANIFEST_LITE / "processed.parquet")
              .groupby("ingested_at")
              .agg(n_files=("source_path", "size"),
                   version=("pipeline_version", "first"))
              .sort_index())

    runs_sorted = sorted(by_run)
    if len(runs_sorted) != len(ingest):
        print(f"★ run_id 有 {len(runs_sorted)} 組、ingested_at 有 {len(ingest)} 組，"
              "對不起來，中止回填")
        return 1

    print(f"{len(runs_sorted)} 組 run_id 與 {len(ingest)} 組 ingested_at 數量相符，開始回填\n")

    for order, (run_id, (ingested_at, ing)) in enumerate(
            zip(runs_sorted, ingest.iterrows()), start=1):
        items = by_run[run_id]
        mtimes = [datetime.datetime.fromtimestamp(p.stat().st_mtime).astimezone()
                  for p, _ in items]
        rows = sum(pq.ParquetFile(p).metadata.num_rows for p, _ in items)
        partitions = sorted({p.parent.name.split("=", 1)[1] for p, _ in items})
        files = sorted(p.relative_to(root).as_posix() for p, _ in items)
        chunks = sorted({c for _, c in items})

        started = pd.Timestamp(ingested_at).tz_convert("Asia/Taipei").to_pydatetime()
        finished = max(mtimes)

        payload = {
            "run_id": run_id,
            "pipeline_version": str(ing["version"]),
            "command": "extract_lite",
            # 逾時被砍的那幾批，程式沒有正常結束；exit_code 記未知而不是編一個。
            "status": "failed" if order in KILLED else "success",
            "stages": ["extract_lite"],
            "published": False,
            "started_at": started.isoformat(timespec="seconds"),
            # 由最後一個 parquet 的 mtime 推得，不是程式記下的結束時間。
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_sec": round((finished - started).total_seconds(), 3),
            # 當時的資料集大小無從得知（這是「執行當下」的快照值）。
            "n_requests": UNKNOWN,
            "n_metrics_run": 0,
            "n_metrics_failed": 0,
            "exit_code": UNKNOWN,

            "backfilled": True,
            "backfilled_at": BACKFILLED_AT,
            "backfill_note": (
                "本檔由 parquet 檔名、_manifest_lite 的 ingested_at 與檔案 mtime "
                "重建，不是執行當下寫下的。標「回填，未知」的欄位無法從輸出反推，"
                "不做推測。finished_at 與 duration_sec 由最後一個 parquet 的 "
                "mtime 推得，屬近似值。"),
            "backfill_purpose": PURPOSE.get(order, UNKNOWN),
            "backfill_sources": [
                "data/01_request_lite/**/part-<run_id>-cNNNN-i.parquet 的檔名與 footer",
                "data/_manifest_lite/processed.parquet 的 ingested_at 分組",
                "parquet 檔案的 mtime",
            ],

            "git_revision": UNKNOWN,
            "raw_root": RAW_ROOT_THEN,
            "raw_root_from_env": True,
            "input": {
                "scanned": UNKNOWN,
                "pending": UNKNOWN,
                "skipped": UNKNOWN,
                "max_files": UNKNOWN,
                "chunk_size": UNKNOWN,
                "chunks": len(chunks),
                "files_ingested": int(ing["n_files"]),
            },
            "output": {
                "rows_written": int(rows),
                "files_processed": int(ing["n_files"]),
                "parquet_files": files,
                "partitions": partitions,
                "remaining": UNKNOWN,
            },
            "dedup": {
                "duplicate_request_id": UNKNOWN,
                "reprocessed_source_files": UNKNOWN,
                "replaced_rows": UNKNOWN,
            },
            "parse_errors": UNKNOWN,
        }

        run_dir = config.RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "run_manifest.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        print(f"  {order}. {run_id}")
        print(f"     {payload['status']:<8} 列數 {rows:>7,}  檔 {len(files):>2}  "
              f"分區 {len(partitions):>2}  chunk {len(chunks)}")
        print(f"     {started:%m-%d %H:%M:%S} ~ {finished:%m-%d %H:%M:%S}"
              f"  ({payload['duration_sec']:.0f}s)")
        print(f"     {PURPOSE.get(order, '')}")

    total = sum(sum(pq.ParquetFile(p).metadata.num_rows for p, _ in by_run[r])
                for r in runs_sorted)
    print(f"\n回填 {len(runs_sorted)} 份，列數合計 {total:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
