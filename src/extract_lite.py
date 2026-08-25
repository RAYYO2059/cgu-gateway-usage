"""lite 匯出的 L1 抽取：原始 JSON → data/01_request_lite 的分區 parquet。

**這是與 clean 並行的另一條資料流，不是 extract.py 的替代品。** 兩者的
schema（``ai_platform_request`` 1.1-lite vs clean）欄位路徑不同、身分鍵不同、
時間戳語意也不同，任何一邊的輸出都不該被當成另一邊來讀。extract.py 完全沒有
被改動，本模組也不 import 它的解析邏輯。

三條在別處會出錯、所以寫死在這裡的規則：

**原始檔不進 repo。** 路徑由環境變數 ``CGU_LITE_RAW`` 指定（見
config.lite_raw_dir()）。lite 的 json 只要落進 ``data/00_raw/`` 底下任何一層，
clean 的 ``extract.py`` 就會用 ``DATA_RAW.rglob("*.json")`` 把它撿走，再用
clean 的欄位路徑解析，產出 12 萬列幾乎全 null 的資料——而 ``request_id`` 在
兩套 schema 裡剛好都是頂層同名欄位、會有值，schema 驗證未必攔得住。

**prompt_text 不落地。** 原始檔含明文提示詞。這裡只寫長度與 sha256：
長度足以做分布分析，雜湊足以判斷「是不是同一份 payload 重送」，而明文一旦
寫進 parquet，就會跟著 data/ 目錄被備份、被複製，再也收不回來。

**分區鍵用 received_at 轉台北時間。** 不是 created_at，也不是來源資料夾名
（那是 UTC 曆日）。三種口徑在全期有 10 筆跨午夜的請求會分到不同日期，其中
一筆（received 23:59:48Z / created 次日 00:00:03Z）正是 08-19 與 08-20 的分界。
clean 的 date_taipei 走的就是 received_at → 台北，對齊它才能兩邊比對。

時間欄位的命名刻意與 clean 一致（ts_utc = received_at、
ts_created_utc = created_at、log_lag_ms = ts_utc - ts_created_utc），
因為那是同一個語意；能對齊的就對齊，對不齊的就換名字，不要似像非像。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src import config

try:
    from zoneinfo import ZoneInfo

    TAIPEI = ZoneInfo(config.TIMEZONE)
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "Windows 沒有系統時區資料庫，zoneinfo 需要 tzdata 套件。\n"
        "請執行：pip install tzdata（或 pip install -r requirements.txt）"
    ) from exc

logger = logging.getLogger(__name__)

UTC = timezone.utc

MANIFEST_PATH = config.MANIFEST_LITE / "processed.parquet"
MANIFEST_COLUMNS = (
    "source_path", "file_mtime", "file_size", "ingested_at", "pipeline_version",
)

# 每個日期資料夾裡的匯出報告，不是請求記錄。混進去會多出 23 列全 null。
PREPARE_REPORT = "_prepare_report.json"

# 輸出欄位順序。date_taipei 會被 pyarrow 抽出去當分區鍵。
COLUMNS: tuple[str, ...] = (
    # 來源
    "source_path", "source_file", "source_record_index", "date_dir", "request_id",
    # 時間（命名與 clean 對齊，語意相同）
    "ts_utc", "ts_taipei", "date_taipei", "hour_taipei", "weekday_taipei",
    "ts_created_utc", "ts_created_taipei", "log_lag_ms",
    # 身分
    "anonymous_user_id", "user_account",
    # 請求
    "endpoint", "provider", "model_requested", "model_returned",
    "status_code", "stream", "latency_ms", "response_length",
    "thread_id", "conversation_id", "request_style",
    # 提示詞：只留長度與雜湊，明文不落地
    "prompt_length", "prompt_text_len", "prompt_text_sha256",
    # 用量
    "prompt_tokens", "cached_tokens", "cache_write_tokens",
    "completion_tokens", "reasoning_tokens", "total_tokens",
    # 代理
    "tool_count", "agent_count",
    # 上游自述
    "schema_name", "schema_version", "cleaner_version", "anonymizer_version",
    "processed_at",
    # 本地
    "ingested_at",
)

DTYPES: dict[str, str] = {
    "source_path": "string", "source_file": "string", "date_dir": "string",
    "request_id": "string", "source_record_index": "Int64",
    "anonymous_user_id": "string", "user_account": "string",
    "endpoint": "string", "provider": "string",
    "model_requested": "string", "model_returned": "string",
    "status_code": "Int64", "stream": "boolean", "latency_ms": "Float64",
    "response_length": "Int64",
    "thread_id": "string", "conversation_id": "string", "request_style": "string",
    "prompt_length": "Int64", "prompt_text_len": "Int64",
    "prompt_text_sha256": "string",
    "prompt_tokens": "Int64", "cached_tokens": "Int64",
    "cache_write_tokens": "Int64", "completion_tokens": "Int64",
    "reasoning_tokens": "Int64", "total_tokens": "Int64",
    "tool_count": "Int64", "agent_count": "Int64",
    "hour_taipei": "Int64", "weekday_taipei": "Int64", "log_lag_ms": "Int64",
    "schema_name": "string", "schema_version": "string",
    "cleaner_version": "string", "anonymizer_version": "string",
    "processed_at": "string",
}


# ---------------------------------------------------------------------------
# 取值
# ---------------------------------------------------------------------------
def _get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _parse_ts(value: Any) -> datetime | None:
    """ISO8601（含結尾 Z）轉 aware datetime。"""
    text = _text(value)
    if text is None:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def flatten(record: dict, source_path: str, date_dir: str,
            ingested_at: datetime) -> dict[str, Any]:
    """一筆 lite JSON → 一列。"""
    ts_utc = _parse_ts(_get(record, "received_at"))
    ts_created_utc = _parse_ts(_get(record, "created_at"))
    ts_taipei = ts_utc.astimezone(TAIPEI) if ts_utc else None
    ts_created_taipei = (
        ts_created_utc.astimezone(TAIPEI) if ts_created_utc else None)

    # 與 clean 同一個公式：received - created。實測為負（上游落地晚於收件），
    # 中位數約 -27.6 秒。它**不是**延遲指標，與 latency_ms 的相關係數僅 0.06；
    # 留著是當上游匯出流程的哨兵，見 schema_lite。
    log_lag_ms = (
        int(round((ts_utc - ts_created_utc).total_seconds() * 1000))
        if ts_utc and ts_created_utc else None
    )

    prompt_text = _get(record, "request.prompt_text")
    has_text = isinstance(prompt_text, str)

    return {
        "source_path": source_path,
        "source_file": _text(_get(record, "source.source_file")),
        "source_record_index": _int(_get(record, "source.record_index")),
        "date_dir": date_dir,
        "request_id": _text(_get(record, "request_id")),

        "ts_utc": ts_utc,
        "ts_taipei": ts_taipei,
        # 分區鍵。用 ts_taipei 推導，不用 date_dir——後者是 UTC 曆日。
        "date_taipei": ts_taipei.date() if ts_taipei else None,
        "hour_taipei": ts_taipei.hour if ts_taipei else None,
        "weekday_taipei": ts_taipei.weekday() if ts_taipei else None,
        "ts_created_utc": ts_created_utc,
        "ts_created_taipei": ts_created_taipei,
        "log_lag_ms": log_lag_ms,

        "anonymous_user_id": _text(_get(record, "identity.anonymous_user_id")),
        "user_account": _text(_get(record, "identity.user_account")),

        "endpoint": _text(_get(record, "request.endpoint")),
        "provider": _text(_get(record, "request.provider")),
        "model_requested": _text(_get(record, "request.model_requested")),
        "model_returned": _text(_get(record, "response.model_returned")),
        "status_code": _int(_get(record, "response.status_code")),
        "stream": _bool(_get(record, "request.stream")),
        "latency_ms": _float(_get(record, "response.latency_ms")),
        "response_length": _int(_get(record, "response.response_length")),
        "thread_id": _text(_get(record, "request.thread_id")),
        "conversation_id": _text(_get(record, "request.conversation_id")),
        "request_style": _text(_get(record, "source.request_style")),

        "prompt_length": _int(_get(record, "request.prompt_length")),
        # 明文不落地：只留長度與雜湊。
        "prompt_text_len": len(prompt_text) if has_text else None,
        "prompt_text_sha256": (
            hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
            if has_text else None),

        "prompt_tokens": _int(_get(record, "usage.prompt_tokens")),
        "cached_tokens": _int(_get(record, "usage.cached_tokens")),
        "cache_write_tokens": _int(_get(record, "usage.cache_write_tokens")),
        "completion_tokens": _int(_get(record, "usage.completion_tokens")),
        "reasoning_tokens": _int(_get(record, "usage.reasoning_tokens")),
        "total_tokens": _int(_get(record, "usage.total_tokens")),

        "tool_count": _int(_get(record, "agent.tool_count")),
        "agent_count": _int(_get(record, "agent.agent_count")),

        "schema_name": _text(_get(record, "schema.name")),
        "schema_version": _text(_get(record, "schema.version")),
        "cleaner_version": _text(_get(record, "processing.cleaner_version")),
        "anonymizer_version": _text(_get(record, "processing.anonymizer_version")),
        "processed_at": _text(_get(record, "processing.processed_at")),

        "ingested_at": ingested_at,
    }


# ---------------------------------------------------------------------------
# 掃描與增量
# ---------------------------------------------------------------------------
def scan_sources(root: Path | None = None) -> list[Path]:
    """遞迴掃描 lite 原始目錄底下的 .json，排除每日的匯出報告。"""
    root = root or config.lite_raw_dir()
    return sorted(p for p in root.rglob("*.json")
                  if p.is_file() and p.name != PREPARE_REPORT)


def rel_path(path: Path, root: Path) -> str:
    """相對 lite 根目錄的路徑，統一用 / 當分隔符，作為 manifest 唯一鍵。"""
    return path.relative_to(root).as_posix()


def date_dir_of(path: Path, root: Path) -> str | None:
    """來源路徑裡的日期資料夾名（UTC 曆日）。只當作來源標記留存，不當分區鍵。"""
    for part in path.relative_to(root).parts:
        if len(part) == 10 and part[4] == "-" and part[7] == "-":
            return part
    return None


def load_manifest() -> pd.DataFrame:
    if not MANIFEST_PATH.exists():
        return pd.DataFrame(columns=list(MANIFEST_COLUMNS))
    try:
        return pd.read_parquet(MANIFEST_PATH)
    except Exception as exc:
        logger.warning("lite manifest 讀取失敗（視為全新處理）：%s: %s",
                       type(exc).__name__, exc)
        return pd.DataFrame(columns=list(MANIFEST_COLUMNS))


def save_manifest(manifest: pd.DataFrame) -> None:
    config.MANIFEST_LITE.mkdir(parents=True, exist_ok=True)
    manifest.to_parquet(MANIFEST_PATH, index=False)


def select_pending(files: Sequence[Path], manifest: pd.DataFrame,
                   root: Path) -> list[Path]:
    """挑出新檔或 (mtime, size) 有變的檔。"""
    known: dict[str, tuple[float, int]] = {}
    if not manifest.empty:
        known = {
            str(r.source_path): (float(r.file_mtime), int(r.file_size))
            for r in manifest.itertuples()
        }

    pending = []
    for path in files:
        stat = path.stat()
        if known.get(rel_path(path, root)) != (stat.st_mtime, stat.st_size):
            pending.append(path)
    return pending


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------
def existing_request_ids() -> set[str]:
    """讀出 01_request_lite 已存在的 request_id，用於跨批次去重。"""
    if not any(config.DATA_REQUEST_LITE.rglob("*.parquet")):
        return set()
    try:
        table = pq.read_table(config.DATA_REQUEST_LITE, columns=["request_id"])
    except Exception as exc:
        logger.warning("既有 lite 輸出讀取失敗（跳過跨批次去重）：%s: %s",
                       type(exc).__name__, exc)
        return set()
    return {v for v in table.column("request_id").to_pylist() if v is not None}


def partition_dirs() -> list[Path]:
    if not config.DATA_REQUEST_LITE.is_dir():
        return []
    return sorted(p for p in config.DATA_REQUEST_LITE.glob("date_taipei=*")
                  if p.is_dir())


def delete_source_paths(source_paths: set[str], run_id: str) -> int:
    """從既有輸出移除這些 source_path 的列，回傳刪除列數。

    與 clean 同一套做法：來源檔 mtime/size 變了代表內容被修正過，舊列必須先
    移除，否則 request_id 去重會把修正後的資料擋掉。parquet 沒有列級刪除，
    只能逐分區讀出、濾掉、整份重寫。
    """
    if not source_paths:
        return 0

    removed = 0
    for part_dir in partition_dirs():
        stale = sorted(part_dir.glob("*.parquet"))
        if not stale:
            continue
        table = pq.read_table(part_dir)
        column = table.column("source_path").to_pylist()
        survivors = [i for i, v in enumerate(column) if v not in source_paths]
        if len(survivors) == len(column):
            continue

        removed += len(column) - len(survivors)
        if survivors:
            tmp = part_dir / f"_rewrite-{run_id}.parquet"
            pq.write_table(table.take(survivors), tmp)
            for old in stale:
                old.unlink()
            tmp.rename(part_dir / f"part-{run_id}-rewrite.parquet")
        else:
            for old in stale:
                old.unlink()
            try:
                part_dir.rmdir()
            except OSError:
                pass

    if removed:
        logger.info("重新處理：從既有 lite 輸出移除 %d 列（來源檔 %d 個）",
                    removed, len(source_paths))
    return removed


def to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    for column, dtype in DTYPES.items():
        frame[column] = frame[column].astype(dtype)
    for column in ("ts_utc", "ts_created_utc", "ingested_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    for column in ("ts_taipei", "ts_created_taipei"):
        frame[column] = pd.to_datetime(frame[column], utc=True).dt.tz_convert(TAIPEI)
    return frame


def write_partitions(frame: pd.DataFrame, run_id: str, chunk_no: int = 0) -> int:
    """依 date_taipei 分區寫入。回傳本次觸及的分區數。

    chunk_no 必須進檔名：同一次 run 分多塊寫入時，若檔名只帶 run_id，
    第二塊的 part-<run_id>-0.parquet 會覆蓋掉第一塊的同名檔，
    而 existing_data_behavior="overwrite_or_ignore" 不會吭聲。
    """
    config.DATA_REQUEST_LITE.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_to_dataset(
        table,
        root_path=str(config.DATA_REQUEST_LITE),
        partition_cols=["date_taipei"],
        existing_data_behavior="overwrite_or_ignore",
        basename_template=f"part-{run_id}-c{chunk_no:04d}-{{i}}.parquet",
    )
    return int(frame["date_taipei"].nunique())


def write_parse_errors(errors: list[tuple[str, str, str]], run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "parse_errors_lite.csv"
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source_path", "error_type", "error_message"])
        writer.writerows(errors)
    return target


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
# 一次寫入的列數上限。
#
# clean 的 extract.py 是「全部讀完再一次寫入」，那是為幾千筆的批次設計的。
# lite 一批 12 萬檔，逐檔讀取要十幾分鐘，中途只要被中斷（逾時、Ctrl+C、
# 機器休眠）就是整批重來、而且一個位元組都沒落地——實測連兩次都是這樣。
#
# 所以改成分塊：每塊寫完就更新 manifest，下次執行靠 select_pending 自動接續。
# 代價是同一分區會多出幾個 parquet 檔，讀取端不受影響。
CHUNK_SIZE = 10_000


def run(run_id: str, chunk_size: int = CHUNK_SIZE,
        max_files: int | None = None) -> dict[str, Any]:
    """抽取一批 lite 原始檔。

    max_files 限制「這次執行最多處理幾個待處理檔」，超出的留給下次。
    用途是把一次十幾分鐘的作業切成幾段，各自落在執行環境的逾時之內；
    因為每塊寫完就存 manifest，下次執行會自動從斷點接續。
    """
    started = time.perf_counter()
    ingested_at = datetime.now(UTC)
    root = config.lite_raw_dir()

    # 一開始就把 run 目錄建起來，不要等到最後寫錯誤檔時才建。
    #
    # config.new_run_id() 的序號是掃 runs/ 底下同日已存在的目錄推出來的。
    # 目錄若拖到執行尾聲才建立，中途死掉的執行就從來不會佔到號碼——實測
    # 這批資料的前四次抽取都被逾時砍掉，於是連續四次都拿到 r003，只靠
    # 時間戳（分鐘精度）維持唯一。同一分鐘內啟動兩次又都沒跑完，run_id
    # 就會完全相同，接著 part-<run_id>-c0000-0.parquet 會互相覆蓋，而
    # existing_data_behavior="overwrite_or_ignore" 不會有任何提示——
    # 跟先前那個分塊檔名沒帶塊序號的 bug 是同一個災難的另一個入口。
    #
    # 副作用是好的：中途死掉會留下一個沒有 run_manifest.json 的空 run 目錄，
    # 那本身就是「跑了但沒跑完」的訊號。
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    files = scan_sources(root)
    manifest = load_manifest()
    pending = select_pending(files, manifest, root)
    n_pending_total = len(pending)
    if max_files is not None and len(pending) > max_files:
        pending = pending[:max_files]
    logger.info("掃描 %d 檔（已排除 %s），待處理 %d 檔，本次處理 %d 檔，跳過 %d 檔",
                len(files), PREPARE_REPORT, n_pending_total, len(pending),
                len(files) - n_pending_total)

    known_paths: set[str] = (
        set() if manifest.empty else set(manifest["source_path"].astype(str)))

    # 既有輸出的 request_id 只讀一次，之後每寫一塊就把新的加進來。
    # 每塊都重讀 parquet 的話，塊數一多就變成 O(n²) 的 I/O。
    already: set[str] = existing_request_ids()

    errors: list[tuple[str, str, str]] = []
    total_processed = 0
    total_kept = 0
    dup_in_batch = 0
    dup_existing = 0
    total_reparsed = 0
    replaced_rows = 0
    partitions: set[str] = set()
    ts_min = ts_max = None

    n_chunks = (len(pending) + chunk_size - 1) // chunk_size if pending else 0
    for chunk_no in range(n_chunks):
        batch = pending[chunk_no * chunk_size:(chunk_no + 1) * chunk_size]
        rows: list[dict[str, Any]] = []
        processed: list[dict[str, Any]] = []
        reparsed: set[str] = set()

        for path in batch:
            relative = rel_path(path, root)
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise TypeError(f"頂層不是 object 而是 {type(record).__name__}")
                rows.append(flatten(record, relative, date_dir_of(path, root),
                                    ingested_at))
            except Exception as exc:
                errors.append((relative, type(exc).__name__, str(exc)[:500]))
                continue
            if relative in known_paths:
                reparsed.add(relative)
            stat = path.stat()
            processed.append({
                "source_path": relative,
                "file_mtime": float(stat.st_mtime),
                "file_size": int(stat.st_size),
                "ingested_at": ingested_at,
                "pipeline_version": config.PIPELINE_VERSION,
            })

        # 去重（第一層）：批次內重複的 request_id 保留先寫入者。
        # seen 用累積的 already，所以跨塊重複也擋得住。
        kept: list[dict[str, Any]] = []
        for row in rows:
            rid = row["request_id"]
            if rid is not None and rid in already:
                dup_in_batch += 1
                continue
            if rid is not None:
                already.add(rid)
            kept.append(row)

        # 重新處理：先移除舊列。順序不可顛倒，否則被取代的舊列會擋掉新列。
        replaced_rows += delete_source_paths(reparsed, run_id)
        total_reparsed += len(reparsed)

        if kept:
            frame = to_frame(kept)
            write_partitions(frame, run_id, chunk_no)
            partitions.update(frame["date_taipei"].astype(str).unique())
            lo, hi = frame["ts_taipei"].min(), frame["ts_taipei"].max()
            ts_min = lo if ts_min is None else min(ts_min, lo)
            ts_max = hi if ts_max is None else max(ts_max, hi)
            total_kept += len(kept)

        # manifest 每塊都存：這是「被中斷之後能接續」的唯一依據。
        if processed:
            fresh = pd.DataFrame(processed)
            if manifest.empty:
                manifest = fresh
            else:
                keep = manifest[~manifest["source_path"].isin(fresh["source_path"])]
                manifest = fresh if keep.empty else pd.concat(
                    [keep, fresh], ignore_index=True)
            save_manifest(manifest)
            total_processed += len(processed)

        logger.info("  第 %d/%d 塊：讀 %d 檔，寫 %d 列（累計 %d/%d）",
                    chunk_no + 1, n_chunks, len(batch), len(kept),
                    total_processed, len(pending))

    if dup_in_batch or dup_existing:
        logger.warning("去重：與既有輸出或批次內重複 %d 筆，皆保留先寫入的那筆",
                       dup_in_batch + dup_existing)

    error_file = write_parse_errors(errors, run_dir)
    elapsed = time.perf_counter() - started

    logger.info("--- L1 lite 抽取報告 ---")
    logger.info("原始目錄     %s", root)
    logger.info("掃描檔案數   %d", len(files))
    logger.info("新處理數     %d（分 %d 塊，每塊上限 %d）",
                total_processed, n_chunks, chunk_size)
    logger.info("跳過數       %d", len(files) - n_pending_total)
    logger.info("尚未處理     %d", n_pending_total - total_processed - len(errors))
    logger.info("解析失敗數   %d（明細：%s）", len(errors), error_file)
    logger.info("重處理檔數   %d（移除舊列 %d）", total_reparsed, replaced_rows)
    logger.info("輸出列數     %d（去重丟棄 %d）",
                total_kept, dup_in_batch + dup_existing)
    logger.info("分區數       %d", len(partitions))
    if ts_min is not None:
        logger.info("時間範圍     %s ~ %s（台北）", ts_min, ts_max)
    logger.info("耗時         %.2f 秒", elapsed)

    if errors:
        logger.warning("以下檔案解析失敗（最多列出 10 筆）：")
        for relative, kind, message in errors[:10]:
            logger.warning("  %s | %s | %s", relative, kind, message)

    return {
        "scanned": len(files),
        "processed": total_processed,
        "skipped": len(files) - n_pending_total,
        "remaining": n_pending_total - total_processed - len(errors),
        "failed": len(errors),
        "rows": total_kept,
        "partitions": len(partitions),
        "chunks": n_chunks,
        "reprocessed": total_reparsed,
        "replaced_rows": replaced_rows,
        "duplicates": dup_in_batch + dup_existing,
        "elapsed_sec": elapsed,
    }


def prune_manifest(root: Path | None = None) -> int:
    """移除 manifest 裡指向已不存在來源檔的記錄，回傳移除筆數。

    來源資料夾被刪掉時，manifest 的那些列不會自己消失。留著大多無害
    （select_pending 只用它判斷「這個路徑處理過沒」），但會讓 manifest 筆數
    與實際檔數對不起來，之後查「到底處理過多少檔」就要先扣掉一批幽靈記錄。

    刻意做成要手動呼叫的動作而不是每次執行都跑：來源檔暫時不在（外接碟沒掛、
    網路磁碟斷線）跟「這批資料不要了」看起來一模一樣，自動清掉會把前者的
    增量狀態一起洗掉，下次得整批重跑。
    """
    root = root or config.lite_raw_dir()
    manifest = load_manifest()
    if manifest.empty:
        return 0

    alive = manifest["source_path"].map(lambda p: (root / p).is_file())
    removed = int((~alive).sum())
    if removed:
        save_manifest(manifest[alive].reset_index(drop=True))
        logger.info("manifest 清理：移除 %d 筆指向已消失來源檔的記錄", removed)
    return removed


def load_dataset() -> pd.DataFrame:
    """讀回 lite L1 輸出。"""
    if not config.DATA_REQUEST_LITE.is_dir() or not any(
            config.DATA_REQUEST_LITE.rglob("*.parquet")):
        raise FileNotFoundError(
            f"{config.DATA_REQUEST_LITE} 沒有資料。\n"
            f"請先設定 {config.LITE_RAW_ENV} 指向 lite 原始目錄，再跑 "
            "python -m src.extract_lite。原始資料不隨 repo 發布。"
        )
    frame = pd.read_parquet(config.DATA_REQUEST_LITE)
    frame["date_taipei"] = frame["date_taipei"].astype("string")
    return frame


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="lite 匯出的 L1 抽取")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE,
                        help=f"每寫入一次的檔數，預設 {CHUNK_SIZE}")
    parser.add_argument("--max-files", type=int, default=None,
                        help="這次執行最多處理幾個待處理檔（其餘留給下次接續）")
    parser.add_argument("--prune-manifest", action="store_true",
                        help="先移除 manifest 裡指向已消失來源檔的記錄再抽取")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config.ensure_dirs()
    if args.prune_manifest:
        prune_manifest()
    result = run(config.new_run_id(), chunk_size=args.chunk_size,
                 max_files=args.max_files)
    return 0 if result["remaining"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
