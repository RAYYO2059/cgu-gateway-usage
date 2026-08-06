"""L1 抽取：把 data/00_raw 底下的原始 JSON 攤平成分區 parquet。

設計原則：
- 所有巢狀取值容錯，缺失一律 None。缺失本身是訊號，不填 0 或空字串。
- 這一步只做三種正規化（時區、空字串 sentinel、內容只留長度），
  分類與衍生欄位留給 L2。
- 增量處理：以 data/_manifest/processed.parquet 記錄 (source_path, mtime, size)。
"""

from __future__ import annotations

import csv
import json
import logging
import time
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from src import config

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    TAIPEI = ZoneInfo(config.TIMEZONE)
except Exception as exc:  # pragma: no cover - 環境問題，不是邏輯問題
    raise RuntimeError(
        f"無法載入時區 {config.TIMEZONE!r}：{type(exc).__name__}: {exc}\n"
        "Windows 沒有系統時區資料庫，zoneinfo 需要 tzdata 套件。\n"
        "請執行：pip install tzdata（或 pip install -r requirements.txt）"
    ) from exc

UTC = timezone.utc

# 空字串等同缺失的欄位：來自不帶對話 ID 的直呼客戶端。
# 若保留空字串，後續 groupby 會把它們全部歸成同一個假群組，而 dropna() 攔不到。
_BLANK_AS_NULL = ("thread_id", "turn_id", "parent_thread_id")

MANIFEST_PATH = config.MANIFEST_DIR / "processed.parquet"
MANIFEST_COLUMNS = (
    "source_path",
    "file_mtime",
    "file_size",
    "ingested_at",
    "pipeline_version",
)

# 輸出欄位順序。date_taipei 會被 pyarrow 抽出去當分區鍵。
COLUMNS: tuple[str, ...] = (
    # 來源
    "source_path", "source_file", "request_id",
    # 時間
    "ts_utc", "ts_taipei", "date_taipei", "hour_taipei", "weekday_taipei",
    # 身分
    "username", "user_account",
    # 請求
    "endpoint", "method", "status_code", "stream", "latency_ms",
    # 模型
    "provider", "worker", "model_requested", "model_returned",
    # 用戶端
    "originator", "user_agent", "host", "remote_addr",
    # 對話
    "thread_id", "session_id", "turn_id", "parent_thread_id", "conversation_key",
    # 用量
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cached_tokens", "cache_write_tokens", "reasoning_tokens",
    # 請求參數
    "tool_choice", "parallel_tool_calls", "store", "prompt_cache_key",
    "tool_count", "tool_types", "instructions_length",
    "reasoning_effort", "reasoning_summary", "reasoning_context",
    "text_verbosity", "text_format_name",
    "client_metadata_keys", "max_completion_tokens", "max_tokens",
    # 回應
    "sse_bytes", "content_type", "truncated", "finish_reasons", "error_type",
    # 內容長度（原文不進表）
    "prompt_len", "assistant_response_len", "memory_len",
    # 訊息
    "messages_format", "message_count",
    "user_message_count", "assistant_message_count",
    # 其他
    "noise_category",
    # 血緣
    "pipeline_version", "ingested_at",
)

# 明確指定 dtype，避免某欄在某批次全為 None 時型別漂移，導致 parquet schema 不一致。
_STRING_COLUMNS = (
    "source_path", "source_file", "request_id",
    "username", "user_account", "endpoint", "method",
    "provider", "worker", "model_requested", "model_returned",
    "originator", "user_agent", "host", "remote_addr",
    "thread_id", "session_id", "turn_id", "parent_thread_id", "conversation_key",
    "tool_choice", "prompt_cache_key", "tool_types",
    "reasoning_effort", "reasoning_summary", "reasoning_context",
    "text_verbosity", "text_format_name", "client_metadata_keys",
    "content_type", "finish_reasons", "error_type",
    "messages_format", "noise_category", "pipeline_version",
)
_INT_COLUMNS = (
    "hour_taipei", "weekday_taipei",
    "status_code", "latency_ms",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cached_tokens", "cache_write_tokens", "reasoning_tokens",
    "tool_count", "instructions_length", "max_completion_tokens", "max_tokens",
    "sse_bytes", "prompt_len", "assistant_response_len", "memory_len",
    "message_count", "user_message_count", "assistant_message_count",
)
_BOOL_COLUMNS = ("stream", "parallel_tool_calls", "store", "truncated")

DTYPES: dict[str, str] = {
    **{c: "string" for c in _STRING_COLUMNS},
    **{c: "Int64" for c in _INT_COLUMNS},
    **{c: "boolean" for c in _BOOL_COLUMNS},
}


# ---------------------------------------------------------------------------
# 容錯取值
# ---------------------------------------------------------------------------
def _get(obj: Any, path: str) -> Any:
    """依 'a.b.c' 取值，中途只要不是 dict 或鍵不存在就回 None。"""
    current = obj
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _first(obj: Any, *paths: str) -> Any:
    """回傳第一個取得到的非 None 值。用於同一語意有多個來源位置的欄位。"""
    for path in paths:
        value = _get(obj, path)
        if value is not None:
            return value
    return None


def _text(value: Any) -> str | None:
    """轉成字串。

    空字串「原樣保留」：noise_category、client.originator 等欄位 100% 存在但值常為 ""，
    那代表「有這個欄位、值為空」，與「沒有這個欄位」是不同的訊號。
    只有 _BLANK_AS_NULL 列出的欄位才把 "" 轉成 None。
    結構化的值序列化保留，而不是丟掉。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _join(value: Any) -> str | None:
    """list → 逗號分隔字串；空 list 保留為空字串以區別於「沒有這個欄位」。"""
    if value is None:
        return None
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return _text(value)


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _length(value: Any) -> int | None:
    """只回傳字元長度，原文不進表（含個資與本機路徑）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False))


def _count(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return _int(value)


def _parse_ts(value: Any) -> datetime | None:
    """解析 ISO 8601 時間字串。無時區資訊者依規格視為 UTC。"""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# 攤平
# ---------------------------------------------------------------------------
def flatten(record: dict, source_path: str, ingested_at: datetime) -> dict[str, Any]:
    """把一筆原始請求紀錄攤平成單層 dict。缺失一律 None。"""
    ts_utc = _parse_ts(_get(record, "time.received_at"))
    ts_taipei = ts_utc.astimezone(TAIPEI) if ts_utc else None

    row: dict[str, Any] = {
        "source_path": source_path,
        "source_file": Path(source_path).name,
        "request_id": _text(_get(record, "request.request_id")),

        "ts_utc": ts_utc,
        "ts_taipei": ts_taipei,
        "date_taipei": ts_taipei.date() if ts_taipei else None,
        "hour_taipei": ts_taipei.hour if ts_taipei else None,
        "weekday_taipei": ts_taipei.weekday() if ts_taipei else None,

        "username": _text(_get(record, "identity.username")),
        "user_account": _text(_get(record, "identity.user_account")),

        "endpoint": _text(_get(record, "request.endpoint")),
        "method": _text(_get(record, "request.method")),
        "status_code": _int(_get(record, "request.status_code")),
        "stream": _bool(_get(record, "request.stream")),
        "latency_ms": _int(_get(record, "request.latency_ms")),

        "provider": _text(_get(record, "model.provider")),
        "worker": _text(_get(record, "model.worker")),
        "model_requested": _text(_get(record, "model.model_requested")),
        "model_returned": _text(_get(record, "model.model_returned")),

        "originator": _text(_get(record, "client.originator")),
        "user_agent": _text(_get(record, "client.user_agent")),
        "host": _text(_get(record, "client.host")),
        "remote_addr": _text(_get(record, "client.remote_addr")),

        "thread_id": _text(_get(record, "conversation.thread_id")),
        "session_id": _text(_get(record, "conversation.session_id")),
        "turn_id": _text(_get(record, "conversation.turn_id")),
        "parent_thread_id": _text(_get(record, "conversation.parent_thread_id")),
        "conversation_key": _text(_get(record, "conversation.conversation_key")),

        "prompt_tokens": _int(_get(record, "usage.prompt_tokens")),
        "completion_tokens": _int(_get(record, "usage.completion_tokens")),
        "total_tokens": _int(_get(record, "usage.total_tokens")),
        # 攤平版與巢狀版並存，優先取攤平版，缺了才往巢狀找。
        "cached_tokens": _int(_first(
            record,
            "usage_details.cached_tokens",
            "usage_details.input_tokens_details.cached_tokens",
        )),
        "cache_write_tokens": _int(_first(
            record,
            "usage_details.cache_write_tokens",
            "usage_details.input_tokens_details.cache_write_tokens",
        )),
        "reasoning_tokens": _int(_first(
            record,
            "usage_details.reasoning_tokens",
            "usage_details.output_tokens_details.reasoning_tokens",
        )),

        "tool_choice": _text(_get(record, "request_options.tool_choice")),
        "parallel_tool_calls": _bool(_get(record, "request_options.parallel_tool_calls")),
        "store": _bool(_get(record, "request_options.store")),
        "prompt_cache_key": _text(_get(record, "request_options.prompt_cache_key")),
        "tool_count": _count(_get(record, "request_options.tool_count")),
        "tool_types": _join(_get(record, "request_options.tool_types")),
        "instructions_length": _int(_get(record, "request_options.instructions_length")),
        "reasoning_effort": _text(_get(record, "request_options.reasoning.effort")),
        "reasoning_summary": _text(_get(record, "request_options.reasoning.summary")),
        "reasoning_context": _text(_get(record, "request_options.reasoning.context")),
        "text_verbosity": _text(_get(record, "request_options.text.verbosity")),
        "text_format_name": _text(_first(
            record,
            "request_options.text.format.name",
            "request_options.text.format.type",
        )),
        "client_metadata_keys": _join(_get(record, "request_options.client_metadata_keys")),
        # max_completion_tokens 與 max_tokens 是兩個不同的 API 參數
        # （/v1/responses 用前者、/v1/chat/completions 用後者），不可合併。
        "max_completion_tokens": _int(_get(record, "request_options.max_completion_tokens")),
        "max_tokens": _int(_get(record, "request_options.max_tokens")),

        "sse_bytes": _int(_get(record, "response_summary.meta.bytes")),
        "content_type": _text(_get(record, "response_summary.meta.content_type")),
        "truncated": _bool(_get(record, "response_summary.meta.truncated")),
        "finish_reasons": _join(_get(record, "response_summary.finish_reasons")),
        "error_type": _text(_first(
            record,
            "response_summary.error.type",
            "response_summary.error",
        )),

        "prompt_len": _length(_get(record, "content.prompt")),
        "assistant_response_len": _length(_get(record, "content.assistant_response")),
        "memory_len": _length(_get(record, "content.memory")),

        "messages_format": _text(_get(record, "messages_summary.format")),
        "message_count": _int(_get(record, "messages_summary.message_count")),
        "user_message_count": _int(_get(record, "messages_summary.user_message_count")),
        "assistant_message_count": _int(
            _get(record, "messages_summary.assistant_message_count")
        ),

        "noise_category": _text(record.get("noise_category")),

        "pipeline_version": config.PIPELINE_VERSION,
        "ingested_at": ingested_at,
    }

    # 空字串 sentinel → None（_text 已處理，這裡明確再確認一次語意）
    for key in _BLANK_AS_NULL:
        if row[key] == "":
            row[key] = None

    return row


# ---------------------------------------------------------------------------
# 來源掃描與 manifest
# ---------------------------------------------------------------------------
def scan_sources() -> list[Path]:
    """遞迴掃描 data/00_raw 底下所有 .json（含日期子目錄）。"""
    return sorted(p for p in config.DATA_RAW.rglob("*.json") if p.is_file())


def rel_path(path: Path) -> str:
    """相對 data/00_raw 的路徑，統一用 / 當分隔符，作為 manifest 唯一鍵。"""
    return path.relative_to(config.DATA_RAW).as_posix()


def load_manifest() -> pd.DataFrame:
    if not MANIFEST_PATH.exists():
        return pd.DataFrame(columns=list(MANIFEST_COLUMNS))
    try:
        return pd.read_parquet(MANIFEST_PATH)
    except Exception as exc:
        logger.warning("manifest 讀取失敗（視為全新處理）：%s: %s", type(exc).__name__, exc)
        return pd.DataFrame(columns=list(MANIFEST_COLUMNS))


def save_manifest(manifest: pd.DataFrame) -> None:
    config.MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest.to_parquet(MANIFEST_PATH, index=False)


def select_pending(files: Sequence[Path], manifest: pd.DataFrame) -> list[Path]:
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
        previous = known.get(rel_path(path))
        if previous is None or previous != (stat.st_mtime, stat.st_size):
            pending.append(path)
    return pending


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------
def existing_request_ids() -> set[str]:
    """讀出 01_request 已存在的 request_id，用於跨批次去重。"""
    if not any(config.DATA_REQUEST.rglob("*.parquet")):
        return set()
    try:
        table = pq.read_table(config.DATA_REQUEST, columns=["request_id"])
    except Exception as exc:
        logger.warning("既有輸出讀取失敗（跳過跨批次去重）：%s: %s", type(exc).__name__, exc)
        return set()
    return {v for v in table.column("request_id").to_pylist() if v is not None}


def to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    for column, dtype in DTYPES.items():
        frame[column] = frame[column].astype(dtype)
    for column in ("ts_utc", "ingested_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame["ts_taipei"] = pd.to_datetime(frame["ts_taipei"], utc=True).dt.tz_convert(TAIPEI)
    return frame


def write_partitions(frame: pd.DataFrame, run_id: str) -> int:
    """依 date_taipei 分區寫入。回傳本次觸及的分區數。"""
    config.DATA_REQUEST.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_to_dataset(
        table,
        root_path=str(config.DATA_REQUEST),
        partition_cols=["date_taipei"],
        existing_data_behavior="overwrite_or_ignore",
        basename_template=f"part-{run_id}-{{i}}.parquet",
    )
    return int(frame["date_taipei"].nunique())


def write_parse_errors(errors: list[tuple[str, str, str]], run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "parse_errors.csv"
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source_path", "error_type", "error_message"])
        writer.writerows(errors)
    return target


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(run_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    ingested_at = datetime.now(UTC)
    run_dir = config.RUNS_DIR / run_id

    files = scan_sources()
    manifest = load_manifest()
    pending = select_pending(files, manifest)
    logger.info(
        "掃描 %d 檔，待處理 %d 檔，跳過 %d 檔",
        len(files), len(pending), len(files) - len(pending),
    )

    rows: list[dict[str, Any]] = []
    errors: list[tuple[str, str, str]] = []
    processed: list[dict[str, Any]] = []

    for path in pending:
        relative = rel_path(path)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise TypeError(f"頂層不是 object 而是 {type(record).__name__}")
            rows.append(flatten(record, relative, ingested_at))
        except Exception as exc:
            # 單檔失敗不中斷整批
            errors.append((relative, type(exc).__name__, str(exc)[:500]))
            continue
        stat = path.stat()
        processed.append({
            "source_path": relative,
            "file_mtime": float(stat.st_mtime),
            "file_size": int(stat.st_size),
            "ingested_at": ingested_at,
            "pipeline_version": config.PIPELINE_VERSION,
        })

    # 去重：批次內先到先留，再擋掉已存在於輸出的 request_id
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    dup_in_batch = 0
    for row in rows:
        rid = row["request_id"]
        if rid is not None and rid in seen:
            dup_in_batch += 1
            continue
        if rid is not None:
            seen.add(rid)
        kept.append(row)

    dup_existing = 0
    if kept:
        already = existing_request_ids()
        if already:
            filtered = [r for r in kept if r["request_id"] not in already]
            dup_existing = len(kept) - len(filtered)
            kept = filtered

    if dup_in_batch or dup_existing:
        logger.warning(
            "去重：批次內重複 %d 筆、與既有輸出重複 %d 筆，皆保留先寫入的那筆",
            dup_in_batch, dup_existing,
        )

    partitions = 0
    ts_min = ts_max = None
    if kept:
        frame = to_frame(kept)
        partitions = write_partitions(frame, run_id)
        ts_min, ts_max = frame["ts_taipei"].min(), frame["ts_taipei"].max()

    if processed:
        fresh = pd.DataFrame(processed)
        if manifest.empty:
            updated = fresh
        else:
            keep = manifest[~manifest["source_path"].isin(fresh["source_path"])]
            updated = fresh if keep.empty else pd.concat([keep, fresh], ignore_index=True)
        save_manifest(updated)

    error_file = write_parse_errors(errors, run_dir)
    elapsed = time.perf_counter() - started

    logger.info("--- L1 抽取報告 ---")
    logger.info("掃描檔案數   %d", len(files))
    logger.info("新處理數     %d", len(processed))
    logger.info("跳過數       %d", len(files) - len(pending))
    logger.info("解析失敗數   %d（明細：%s）", len(errors), error_file)
    logger.info("輸出列數     %d（去重丟棄 %d）", len(kept), dup_in_batch + dup_existing)
    logger.info("分區數       %d", partitions)
    if ts_min is not None:
        logger.info("時間範圍     %s ~ %s（台北）", ts_min, ts_max)
    logger.info("耗時         %.2f 秒", elapsed)

    if errors:
        logger.warning("以下檔案解析失敗（最多列出 10 筆）：")
        for relative, kind, message in errors[:10]:
            logger.warning("  %s | %s | %s", relative, kind, message)

    return {
        "scanned": len(files),
        "processed": len(processed),
        "skipped": len(files) - len(pending),
        "failed": len(errors),
        "rows": len(kept),
        "partitions": partitions,
        "duplicates": dup_in_batch + dup_existing,
        "elapsed_sec": elapsed,
    }
