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
import re
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

# 空字串轉 None 的欄位清單。判準是「空字串代表什麼」，不是「哪些欄位有空字串」：
#
#   ID 類欄位（此清單）—— 空字串是 sentinel。來自不帶對話 ID 的直呼客戶端，
#   語意等同「這次請求沒有這個 ID」。若保留原樣，groupby 會把成千上萬筆
#   不相干的請求併成同一個假群組，而 dropna() 攔不到空字串，是靜默錯誤。
#
#   狀態類欄位（noise_category、client.originator、user_agent 等）—— 空字串
#   是有意義的值，代表「已判定，結果為空/無」，與「沒有這個欄位」不同。
#   一律原樣保留，不進這份清單。
_BLANK_AS_NULL = ("thread_id", "session_id", "turn_id", "parent_thread_id")

# 模型名尾端的版本日期後綴，如 gpt-5.4-mini-2026-03-17 → gpt-5.4-mini。
# 不剝掉會怎樣：同一個模型的不同快照被當成不同模型，
# 「請求的模型 vs 實際服務的模型」比對會把版本標註誤判成模型替換。
_MODEL_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")

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
    "source_path", "source_file", "source_file_original", "request_id",
    # 時間
    "ts_utc", "ts_taipei", "date_taipei", "hour_taipei", "weekday_taipei",
    "ts_created_utc", "ts_created_taipei", "log_lag_ms",
    # 身分
    "username", "user_account",
    # 請求
    "endpoint", "method", "status_code", "stream", "latency_ms",
    # 模型
    "provider", "worker", "model_requested", "model_returned", "model_family",
    "response_model",
    # 用戶端
    "originator", "user_agent", "host", "remote_addr", "client_type",
    # 對話
    "thread_id", "session_id", "turn_id", "parent_thread_id", "conversation_key",
    # 用量
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cached_tokens", "cache_write_tokens", "reasoning_tokens",
    "input_text_tokens", "input_audio_tokens", "input_image_tokens",
    "output_text_tokens", "output_audio_tokens", "output_image_tokens",
    "accepted_prediction_tokens", "rejected_prediction_tokens", "usage_missing",
    # 請求參數
    "tool_choice", "parallel_tool_calls", "store", "prompt_cache_key", "temperature",
    "tool_count", "tool_types", "instructions_length",
    "reasoning_effort", "reasoning_summary", "reasoning_context",
    "text_verbosity", "text_format_name",
    "client_metadata_keys", "max_completion_tokens", "max_tokens",
    "include_options", "request_stream_option",
    # 回應
    "sse_bytes", "content_type", "truncated", "finish_reasons",
    "error_type", "error_code", "error_param",
    # 影像請求（image_request 幾乎全為空 dict，只有極少數請求有內容）
    "image_model", "image_n", "image_quality", "image_size",
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

# 輸出欄位 → 來源 JSON 路徑。給 audit.py 做雙向稽核用：
#   正向：原始 JSON 有、這裡沒宣告的路徑 → 上游新增了欄位而我們靜默忽略。
#   反向：這裡宣告了、原始 JSON 沒有的路徑 → 上游移除了欄位，或這是死路徑。
#
# 一欄多路徑代表 _first() 的 fallback 順序（攤平版優先、巢狀版次之）。
# 純衍生欄位標 DERIVED，不參與比對——它們沒有對應的來源路徑，
# 硬要比對只會在兩個方向都產生假警報。
#
# 這份對照表必須與 flatten() 手動同步。audit.py 會檢查它與 COLUMNS 的鍵一致，
# 但檢查不到「路徑寫錯成另一個存在的路徑」——那只能靠 review。
DERIVED: tuple[str, ...] = ()

COLUMN_SOURCE_MAP: dict[str, tuple[str, ...]] = {
    # 來源
    "source_path": DERIVED,              # 由檔案位置決定
    "source_file": DERIVED,              # 由檔案位置決定
    "source_file_original": ("source_file",),
    "request_id": ("request.request_id",),
    # 時間
    "ts_utc": ("time.received_at",),
    "ts_taipei": DERIVED,
    "date_taipei": DERIVED,
    "hour_taipei": DERIVED,
    "weekday_taipei": DERIVED,
    "ts_created_utc": ("time.created_at",),
    "ts_created_taipei": DERIVED,
    "log_lag_ms": DERIVED,
    # 身分
    "username": ("identity.username",),
    "user_account": ("identity.user_account",),
    # 請求
    "endpoint": ("request.endpoint",),
    "method": ("request.method",),
    "status_code": ("request.status_code",),
    "stream": ("request.stream",),
    "latency_ms": ("request.latency_ms",),
    # 模型
    "provider": ("model.provider",),
    "worker": ("model.worker",),
    "model_requested": ("model.model_requested",),
    "model_returned": ("model.model_returned",),
    "model_family": DERIVED,
    # 第三個模型欄位，位於回應摘要而非 model 區塊。抽它的唯一理由是
    # 它與 model.model_returned 來自不同的上游環節，可以互相勾稽——
    # 現有的「模型替換率」是拿 gateway 自己的兩個欄位比出來的，無法自我驗證。
    "response_model": ("response_summary.model",),
    # 用戶端
    "originator": ("client.originator",),
    "user_agent": ("client.user_agent",),
    "host": ("client.host",),
    "remote_addr": ("client.remote_addr",),
    "client_type": DERIVED,
    # 對話
    "thread_id": ("conversation.thread_id",),
    "session_id": ("conversation.session_id",),
    "turn_id": ("conversation.turn_id",),
    "parent_thread_id": ("conversation.parent_thread_id",),
    "conversation_key": ("conversation.conversation_key",),
    # 用量
    "prompt_tokens": ("usage.prompt_tokens",),
    "completion_tokens": ("usage.completion_tokens",),
    "total_tokens": ("usage.total_tokens",),
    "cached_tokens": (
        "usage_details.cached_tokens",
        "usage_details.input_tokens_details.cached_tokens",
    ),
    "cache_write_tokens": (
        "usage_details.cache_write_tokens",
        "usage_details.input_tokens_details.cache_write_tokens",
    ),
    "reasoning_tokens": (
        "usage_details.reasoning_tokens",
        "usage_details.output_tokens_details.reasoning_tokens",
    ),
    "input_text_tokens": (
        "usage_details.input_text_tokens",
        "usage_details.input_tokens_details.text_tokens",
    ),
    "input_audio_tokens": (
        "usage_details.input_audio_tokens",
        "usage_details.input_tokens_details.audio_tokens",
    ),
    "input_image_tokens": (
        "usage_details.input_image_tokens",
        "usage_details.input_tokens_details.image_tokens",
    ),
    "output_text_tokens": (
        "usage_details.output_text_tokens",
        "usage_details.output_tokens_details.text_tokens",
    ),
    "output_audio_tokens": (
        "usage_details.output_audio_tokens",
        "usage_details.output_tokens_details.audio_tokens",
    ),
    "output_image_tokens": (
        "usage_details.output_image_tokens",
        "usage_details.output_tokens_details.image_tokens",
    ),
    "accepted_prediction_tokens": (
        "usage_details.accepted_prediction_tokens",
        "usage_details.output_tokens_details.accepted_prediction_tokens",
    ),
    "rejected_prediction_tokens": (
        "usage_details.rejected_prediction_tokens",
        "usage_details.output_tokens_details.rejected_prediction_tokens",
    ),
    "usage_missing": DERIVED,            # 由 usage_details 是否為空 dict 判定
    # 請求參數
    "tool_choice": ("request_options.tool_choice",),
    "parallel_tool_calls": ("request_options.parallel_tool_calls",),
    "store": ("request_options.store",),
    "prompt_cache_key": ("request_options.prompt_cache_key",),
    "temperature": ("request_options.temperature",),
    "tool_count": ("request_options.tool_count",),
    "tool_types": ("request_options.tool_types",),
    "instructions_length": ("request_options.instructions_length",),
    "reasoning_effort": ("request_options.reasoning.effort",),
    "reasoning_summary": ("request_options.reasoning.summary",),
    "reasoning_context": ("request_options.reasoning.context",),
    "text_verbosity": ("request_options.text.verbosity",),
    # .name 這個鍵確實存在（稽核實測 8.3% 的檔有），但值常為 null，
    # 所以 _first() 多半會落到 .type。兩條都要宣告，否則 .name 會被誤報成未映射。
    "text_format_name": (
        "request_options.text.format.name",
        "request_options.text.format.type",
    ),
    "client_metadata_keys": ("request_options.client_metadata_keys",),
    "max_completion_tokens": ("request_options.max_completion_tokens",),
    "max_tokens": ("request_options.max_tokens",),
    "include_options": ("request_options.include",),
    # request.stream 是 gateway 觀察到的實際串流行為，
    # request_options.stream 是客戶端送出的參數。兩者不必然相同，分開存才能比對。
    "request_stream_option": ("request_options.stream",),
    # 回應
    "sse_bytes": ("response_summary.meta.bytes",),
    "content_type": ("response_summary.meta.content_type",),
    "truncated": ("response_summary.meta.truncated",),
    "finish_reasons": ("response_summary.finish_reasons",),
    "error_type": ("response_summary.error.type", "response_summary.error"),
    "error_code": ("response_summary.error.code",),
    "error_param": ("response_summary.error.param",),
    # 影像請求
    "image_model": ("image_request.model",),
    "image_n": ("image_request.n",),
    "image_quality": ("image_request.quality",),
    "image_size": ("image_request.size",),
    # 內容長度（只取長度，原文不進表）
    "prompt_len": ("content.prompt",),
    "assistant_response_len": ("content.assistant_response",),
    "memory_len": ("content.memory",),
    # 訊息
    "messages_format": ("messages_summary.format",),
    "message_count": ("messages_summary.message_count",),
    "user_message_count": ("messages_summary.user_message_count",),
    "assistant_message_count": ("messages_summary.assistant_message_count",),
    # 其他
    "noise_category": ("noise_category",),
    # 血緣
    "pipeline_version": DERIVED,
    "ingested_at": DERIVED,
}

# 明確指定 dtype，避免某欄在某批次全為 None 時型別漂移，導致 parquet schema 不一致。
_STRING_COLUMNS = (
    "source_path", "source_file", "source_file_original", "request_id",
    "username", "user_account", "endpoint", "method",
    "provider", "worker", "model_requested", "model_returned", "model_family",
    "response_model",
    "originator", "user_agent", "host", "remote_addr", "client_type",
    "thread_id", "session_id", "turn_id", "parent_thread_id", "conversation_key",
    "tool_choice", "prompt_cache_key", "tool_types",
    "reasoning_effort", "reasoning_summary", "reasoning_context",
    "text_verbosity", "text_format_name", "client_metadata_keys", "include_options",
    "content_type", "finish_reasons", "error_type", "error_code", "error_param",
    "image_model", "image_quality", "image_size", "model_family", "client_type",
    "messages_format", "noise_category", "pipeline_version",
)
_INT_COLUMNS = (
    "hour_taipei", "weekday_taipei", "log_lag_ms",
    "status_code", "latency_ms",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cached_tokens", "cache_write_tokens", "reasoning_tokens",
    "input_text_tokens", "input_audio_tokens", "input_image_tokens",
    "output_text_tokens", "output_audio_tokens", "output_image_tokens",
    "accepted_prediction_tokens", "rejected_prediction_tokens",
    "tool_count", "instructions_length", "max_completion_tokens", "max_tokens",
    "sse_bytes", "prompt_len", "assistant_response_len", "memory_len",
    "message_count", "user_message_count", "assistant_message_count",
    "image_n",
)
_BOOL_COLUMNS = ("stream", "parallel_tool_calls", "store", "truncated", "usage_missing",
                 "request_stream_option")
# temperature 在原始資料裡 int/float 混型（0 與 0.3 並存），必須走浮點通道，
# 走 Int64 會把 0.3 截成 0。
_FLOAT_COLUMNS = ("temperature",)

DTYPES: dict[str, str] = {
    **{c: "string" for c in _STRING_COLUMNS},
    **{c: "Int64" for c in _INT_COLUMNS},
    **{c: "boolean" for c in _BOOL_COLUMNS},
    **{c: "Float64" for c in _FLOAT_COLUMNS},
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


def _float(value: Any) -> float | None:
    """bool 是 int 的子類，必須先擋掉，否則 True 會變成 1.0。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
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
    ts_created_utc = _parse_ts(_get(record, "time.created_at"))
    ts_created_taipei = ts_created_utc.astimezone(TAIPEI) if ts_created_utc else None
    # received_at - created_at。兩者缺一即為 None，不補 0。
    #
    # 命名注意：這**不是**排隊時間。實測與 latency_ms 的相關係數僅 0.007，
    # 且偏移集中在 17.5~20.8 秒的窄帶（9,937 筆全為負值），是日誌落地端的
    # 固定延遲，不是請求在 gateway 內等待的時間。
    # 保留它的用途是偵測上游架構變動（見 schema.run_warning_checks），
    # 不可當作效能指標使用。
    log_lag_ms = (
        int(round((ts_utc - ts_created_utc).total_seconds() * 1000))
        if ts_utc and ts_created_utc
        else None
    )

    # usage_details 為空 dict（或整個缺席）時為 True。
    # 這裡直接看原始結構，而不是從 total_tokens==0 且 cached_tokens.isna()
    # 之類的組合條件反推——那種寫法遲早有人漏掉一個條件而算錯用量。
    usage_details = record.get("usage_details")
    usage_missing = not isinstance(usage_details, dict) or not usage_details

    model_returned = _text(_get(record, "model.model_returned"))
    model_family = (
        _MODEL_DATE_SUFFIX.sub("", model_returned) if model_returned else None
    )

    row: dict[str, Any] = {
        "source_path": source_path,
        # source_file 是實際落地的檔名；source_file_original 是 JSON 內自述的檔名，
        # 兩者不一定相同（落地檔多了 .clean 中綴），分開存才能對得起來源系統。
        "source_file": Path(source_path).name,
        "source_file_original": _text(record.get("source_file")),
        "request_id": _text(_get(record, "request.request_id")),

        "ts_utc": ts_utc,
        "ts_taipei": ts_taipei,
        "date_taipei": ts_taipei.date() if ts_taipei else None,
        "hour_taipei": ts_taipei.hour if ts_taipei else None,
        "weekday_taipei": ts_taipei.weekday() if ts_taipei else None,
        "ts_created_utc": ts_created_utc,
        "ts_created_taipei": ts_created_taipei,
        "log_lag_ms": log_lag_ms,

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
        "model_returned": model_returned,
        "model_family": model_family,
        "response_model": _text(_get(record, "response_summary.model")),

        "originator": _text(_get(record, "client.originator")),
        "user_agent": _text(_get(record, "client.user_agent")),
        "host": _text(_get(record, "client.host")),
        "remote_addr": _text(_get(record, "client.remote_addr")),
        # client_type 在下面依正規化後的 thread_id 決定，這裡先佔位。
        "client_type": None,

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
        "input_text_tokens": _int(_first(
            record,
            "usage_details.input_text_tokens",
            "usage_details.input_tokens_details.text_tokens",
        )),
        "input_audio_tokens": _int(_first(
            record,
            "usage_details.input_audio_tokens",
            "usage_details.input_tokens_details.audio_tokens",
        )),
        "input_image_tokens": _int(_first(
            record,
            "usage_details.input_image_tokens",
            "usage_details.input_tokens_details.image_tokens",
        )),
        "output_text_tokens": _int(_first(
            record,
            "usage_details.output_text_tokens",
            "usage_details.output_tokens_details.text_tokens",
        )),
        "output_audio_tokens": _int(_first(
            record,
            "usage_details.output_audio_tokens",
            "usage_details.output_tokens_details.audio_tokens",
        )),
        "output_image_tokens": _int(_first(
            record,
            "usage_details.output_image_tokens",
            "usage_details.output_tokens_details.image_tokens",
        )),
        "accepted_prediction_tokens": _int(_first(
            record,
            "usage_details.accepted_prediction_tokens",
            "usage_details.output_tokens_details.accepted_prediction_tokens",
        )),
        "rejected_prediction_tokens": _int(_first(
            record,
            "usage_details.rejected_prediction_tokens",
            "usage_details.output_tokens_details.rejected_prediction_tokens",
        )),
        "usage_missing": usage_missing,

        "tool_choice": _text(_get(record, "request_options.tool_choice")),
        "parallel_tool_calls": _bool(_get(record, "request_options.parallel_tool_calls")),
        "store": _bool(_get(record, "request_options.store")),
        "prompt_cache_key": _text(_get(record, "request_options.prompt_cache_key")),
        "temperature": _float(_get(record, "request_options.temperature")),
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
        # include 是 list（如 ["reasoning.encrypted_content"]），串成字串保存。
        # 用來佐證 reasoning_effort 是客戶端預設而非使用者調整過的設定。
        "include_options": _join(_get(record, "request_options.include")),
        "request_stream_option": _bool(_get(record, "request_options.stream")),

        "sse_bytes": _int(_get(record, "response_summary.meta.bytes")),
        "content_type": _text(_get(record, "response_summary.meta.content_type")),
        "truncated": _bool(_get(record, "response_summary.meta.truncated")),
        "finish_reasons": _join(_get(record, "response_summary.finish_reasons")),
        "error_type": _text(_first(
            record,
            "response_summary.error.type",
            "response_summary.error",
        )),
        # 只取 code 與 param，不取 error.message：訊息會回帶使用者送出的內容片段。
        # param 是出錯的參數路徑（如 input[1].content[0]），不含內容值。
        "error_code": _text(_get(record, "response_summary.error.code")),
        "error_param": _text(_get(record, "response_summary.error.param")),

        "image_model": _text(_get(record, "image_request.model")),
        "image_n": _int(_get(record, "image_request.n")),
        "image_quality": _text(_get(record, "image_request.quality")),
        "image_size": _text(_get(record, "image_request.size")),

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

    # 空字串 sentinel → None。這是唯一做這件事的地方：
    # _text() 刻意原樣保留空字串，只有 _BLANK_AS_NULL 列出的 ID 欄位的 "" 才代表缺失。
    for key in _BLANK_AS_NULL:
        if row[key] == "":
            row[key] = None

    # client_type 必須在空字串正規化「之後」才算，否則直呼客戶端的 thread_id=""
    # 會被當成有值，整批被誤標成 codex。
    # 已驗證 thread_id / turn_id / reasoning_effort / text_verbosity /
    # parallel_tool_calls / client_metadata_keys / store / prompt_cache_key
    # 這八欄覆蓋率完全一致（71.6%），是同一組客戶端的指紋，用 thread_id 代表即可。
    row["client_type"] = "direct" if row["thread_id"] is None else "codex"

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


def partition_dirs() -> list[Path]:
    """既有輸出的分區目錄（date_taipei=YYYY-MM-DD）。"""
    if not config.DATA_REQUEST.is_dir():
        return []
    return sorted(p for p in config.DATA_REQUEST.glob("date_taipei=*") if p.is_dir())


def delete_source_paths(source_paths: set[str], run_id: str) -> int:
    """從既有輸出移除這些 source_path 的列，回傳刪除列數。

    用於「來源檔重新處理」：檔案 mtime/size 變了代表內容被修正過，
    舊列必須先移除，新列才寫得進去（否則 request_id 去重會擋掉修正後的資料）。

    parquet 沒有列級刪除，做法是逐分區讀出、濾掉、整份重寫。
    重寫先落暫存檔再替換，中途失敗不會留下半毀的分區。
    """
    if not source_paths:
        return 0

    removed = 0
    for part_dir in partition_dirs():
        stale = sorted(part_dir.glob("*.parquet"))
        if not stale:
            continue
        # 直接以分區目錄為 root 讀取，pyarrow 不會把目錄名當分區鍵，
        # 讀回來的 schema 剛好就是「不含 date_taipei」的欄位集合。
        table = pq.read_table(part_dir)
        column = table.column("source_path").to_pylist()
        survivors = [i for i, value in enumerate(column) if value not in source_paths]
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
            # 整個分區都被移除；留空目錄會讓後續讀取拿到 0 檔案的分區，直接清掉。
            for old in stale:
                old.unlink()
            try:
                part_dir.rmdir()
            except OSError:
                pass

    if removed:
        logger.info("重新處理：從既有輸出移除 %d 列（來源檔 %d 個）", removed, len(source_paths))
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

    # 已在 manifest 裡卻仍被挑出來的，代表 mtime/size 變過 → 是重新處理而非新檔。
    known_paths: set[str] = (
        set() if manifest.empty else set(manifest["source_path"].astype(str))
    )

    rows: list[dict[str, Any]] = []
    errors: list[tuple[str, str, str]] = []
    processed: list[dict[str, Any]] = []
    reparsed: set[str] = set()

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
        # 只有「解析成功」的重處理檔才排定刪除舊列。
        # 若這次解析失敗就刪掉舊列，會拿一筆暫時性錯誤換掉一筆好資料。
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

    # 去重（第一層）：同批次內重複的 request_id 保留先寫入者
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

    # 重新處理：先移除舊列，再讀既有 request_id。順序不可顛倒，
    # 否則被取代的舊列會出現在 already 裡，把修正後的新列擋掉。
    replaced_rows = delete_source_paths(reparsed, run_id)

    # 去重（第二層）：擋掉已存在於輸出的 request_id
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
    logger.info("重處理檔數   %d（移除舊列 %d）", len(reparsed), replaced_rows)
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
        "reprocessed": len(reparsed),
        "replaced_rows": replaced_rows,
        "duplicates": dup_in_batch + dup_existing,
        "elapsed_sec": elapsed,
    }
