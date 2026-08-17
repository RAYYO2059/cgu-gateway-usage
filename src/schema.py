"""L1 輸出的資料契約。

這個模組把 extract.py 裡「隱性成立」的規則寫成明文，讓每次執行自動檢查。
它驗證的是**落地後的 parquet**（read_parquet 之後的形狀），不是寫入前的
DataFrame——因為下游拿到的就是前者，契約要對著下游看得到的東西訂。

兩類檢查：
- REQUEST_SCHEMA：不符即 raise，中止流程。用於「一定是錯」的情況。
- run_warning_checks()：只記 log。用於「可能是上游變了，需要人看一眼」的情況。
  這類不該中止，否則上游加一個新 endpoint 就會讓整條 pipeline 停擺。

欄位清單刻意在這裡重新列一次，不從 extract.py import。
若兩邊漂移，check_columns_match() 會抓到——這正是要的：
schema 若由被驗證的程式推導出來，就驗不到「dtype 對照表被改壞」這種錯。
"""

from __future__ import annotations

import logging

import pandas as pd
import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema

from src import config

logger = logging.getLogger(__name__)

TAIPEI_DTYPE = pd.DatetimeTZDtype(tz=config.TIMEZONE)
UTC_DTYPE = pd.DatetimeTZDtype(tz="UTC")

# 空字串 sentinel 欄位。與 extract._BLANK_AS_NULL 對應，這裡獨立列出。
BLANK_FORBIDDEN = ("thread_id", "session_id", "turn_id", "parent_thread_id")


def _check_blank_forbidden_matches() -> None:
    """BLANK_FORBIDDEN 與 extract._BLANK_AS_NULL 必須是同一組欄位。

    與 check_columns_match() 同一個道理：同一份契約寫在兩個地方，
    只改一邊必須立刻失敗，不能安靜地漂開。

    漂開的後果是不對稱的，而且兩邊都不好查：extract 多一欄，那一欄的 ""
    會被轉成 None 但 schema 不禁止空字串，於是驗證通過、資料卻已經被動過；
    schema 多一欄，schema 禁止 "" 而 extract 從沒轉過，於是整批資料驗證失敗、
    錯誤訊息卻指向資料而不是指向這份清單。

    刻意在 import 階段跑：這是兩份程式碼之間的契約，不是資料的性質，
    沒有理由等到有人呼叫某個函數才發現。
    """
    from src import extract

    declared = set(BLANK_FORBIDDEN)
    applied = set(extract._BLANK_AS_NULL)
    if declared != applied:
        raise ValueError(
            "schema.BLANK_FORBIDDEN 與 extract._BLANK_AS_NULL 已經漂開：\n"
            f"  只在 schema.BLANK_FORBIDDEN：{sorted(declared - applied)}\n"
            f"  只在 extract._BLANK_AS_NULL：{sorted(applied - declared)}\n"
            "extract 把哪些欄位的空字串轉成 None，schema 就該禁止哪些欄位"
            "出現空字串。改了一邊請同步改另一邊。"
        )


_check_blank_forbidden_matches()

# log_lag_ms 的預期中位數。來自 9,937 筆的實測：-20,126ms，全距 -20.8s ~ -17.5s。
LOG_LAG_EXPECTED_MEDIAN_MS = -20_000
LOG_LAG_TOLERANCE_MS = 5_000

# 已知的類別值。新值不是錯，但要有人確認語意，所以只警告。
KNOWN_ENDPOINTS = frozenset({
    "/v1/responses", "/v1/chat/completions",
    "/v1/audio/speech", "/v1/audio/transcriptions",
    "/v1/images/generations", "/v1/images/edits",
})
KNOWN_PROVIDERS = frozenset({"openai", "ollama"})
KNOWN_ORIGINATORS = frozenset({
    "", "Codex Desktop", "codex-tui", "codex_exec", "codex_vscode",
    "codex_work_desktop",
})
KNOWN_CLIENT_TYPES = frozenset({"codex", "direct"})


# ---------------------------------------------------------------------------
# 自訂檢查
# ---------------------------------------------------------------------------
def _no_blank(series: pd.Series) -> pd.Series:
    """不做會怎樣：空字串是「沒有這個 ID」的 sentinel。留著它，之後
    groupby(thread_id) 會把數千筆不相干的請求併成同一個假群組，而
    dropna() 攔不到空字串——統計數字會錯得很安靜、很難查。
    """
    return series.fillna("\x00") != ""


def _wall_clock_delta_is_8h(frame: pd.DataFrame) -> pd.Series:
    """不做會怎樣：ts_taipei 若是把 UTC 直接貼上 +08:00 標籤（而非真的換算），
    兩欄的「瞬時」會相同但牆鐘差為 0；反過來若換算方向寫反，差會是 -8 小時。
    兩種錯都會讓 hour_taipei / date_taipei 全盤偏移，且從 dtype 看不出來。
    """
    delta = (frame["ts_taipei"].dt.tz_localize(None)
             - frame["ts_utc"].dt.tz_localize(None))
    return delta == pd.Timedelta(hours=8)


def _date_matches_ts_taipei(frame: pd.DataFrame) -> pd.Series:
    """不做會怎樣：分區鍵若不是從 ts_taipei 推導（例如誤用來源目錄的日期），
    跨 UTC 日界的請求會落在錯誤分區。實測有 19.8% 的列來源目錄日期與
    date_taipei 不同——用錯來源的話，每日用量統計會系統性偏移。
    """
    expected = frame["ts_taipei"].dt.strftime("%Y-%m-%d")
    return frame["date_taipei"].astype("string") == expected.astype("string")


def _client_type_matches_thread(frame: pd.DataFrame) -> pd.Series:
    """不做會怎樣：client_type 是下游切分「Codex 客戶端 vs 直呼」的唯一依據，
    一旦它與 thread_id 脫鉤，兩邊的母數就對不起來而沒人會發現。
    """
    expected = frame["thread_id"].isna().map({True: "direct", False: "codex"})
    return frame["client_type"].astype("string") == expected.astype("string")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def _string(nullable: bool = True, **kwargs) -> Column:
    return Column("string", nullable=nullable, **kwargs)


def _int(nullable: bool = True, checks: list | None = None) -> Column:
    return Column("Int64", nullable=nullable, checks=checks)


def _bool(nullable: bool = True) -> Column:
    return Column("boolean", nullable=nullable)


_NON_NEGATIVE = [Check.ge(0, name="non_negative")]

REQUEST_SCHEMA = DataFrameSchema(
    {
        # --- 來源 ---
        "source_path": _string(nullable=False),
        "source_file": _string(nullable=False),
        "source_file_original": _string(nullable=False),
        # 檢查 4：request_id 唯一且非 null。
        # 不做會怎樣：增量寫入的去重完全建立在 request_id 上，
        # 一旦重複或為 null，同一筆請求會被重複計費／重複統計。
        "request_id": _string(nullable=False, unique=True),

        # --- 時間 ---
        # 檢查 2：dtype 直接指定帶時區的型別，同時擋掉 naive datetime 與錯誤時區。
        "ts_utc": Column(UTC_DTYPE, nullable=False),
        "ts_taipei": Column(TAIPEI_DTYPE, nullable=False),
        # 檢查 5：非 null（分區鍵為 null 會讓那些列落進 __HIVE_DEFAULT_PARTITION__）
        "date_taipei": _string(nullable=False),
        "hour_taipei": _int(nullable=False, checks=[Check.in_range(0, 23)]),
        "weekday_taipei": _int(nullable=False, checks=[Check.in_range(0, 6)]),
        "ts_created_utc": Column(UTC_DTYPE, nullable=True),
        "ts_created_taipei": Column(TAIPEI_DTYPE, nullable=True),
        "log_lag_ms": _int(nullable=True),

        # --- 身分 ---
        # 檢查 8：username 是使用者鍵，null 會讓該列從所有人層級統計中消失。
        "username": _string(nullable=False),
        "user_account": _string(nullable=False),

        # --- 請求 ---
        "endpoint": _string(nullable=False),
        "method": _string(nullable=False),
        # 檢查 7：HTTP 狀態碼範圍。不做會怎樣：上游若改成回傳 -1 或 0 表示
        # 「未完成」，成功率統計會把它算成非 2xx 以外的第三類而不自知。
        "status_code": _int(nullable=False, checks=[Check.in_range(100, 599)]),
        "stream": _bool(nullable=False),
        "latency_ms": _int(nullable=False, checks=_NON_NEGATIVE),

        # --- 模型 ---
        "provider": _string(nullable=False),
        "worker": _string(nullable=False),
        "model_requested": _string(),
        "model_returned": _string(),
        "model_family": _string(),
        "response_model": _string(),

        # --- 用戶端 ---
        "originator": _string(nullable=False),
        "user_agent": _string(nullable=False),
        "host": _string(nullable=False),
        "remote_addr": _string(nullable=False),
        "client_type": _string(nullable=False,
                               checks=[Check.isin(sorted(KNOWN_CLIENT_TYPES))]),

        # --- 對話 ---
        # 檢查 1：四個 ID 欄不得出現空字串。
        "thread_id": _string(checks=[Check(_no_blank, name="no_blank_sentinel")]),
        "session_id": _string(checks=[Check(_no_blank, name="no_blank_sentinel")]),
        "turn_id": _string(checks=[Check(_no_blank, name="no_blank_sentinel")]),
        "parent_thread_id": _string(checks=[Check(_no_blank, name="no_blank_sentinel")]),
        "conversation_key": _string(nullable=False),

        # --- 用量 ---
        # 檢查 6：三個主要 token 欄非負。不做會怎樣：上游用 -1 表示「未知」是
        # 常見做法，混進 sum() 會讓總用量無聲地少算。
        "prompt_tokens": _int(nullable=False, checks=_NON_NEGATIVE),
        "completion_tokens": _int(nullable=False, checks=_NON_NEGATIVE),
        "total_tokens": _int(nullable=False, checks=_NON_NEGATIVE),
        "cached_tokens": _int(checks=_NON_NEGATIVE),
        "cache_write_tokens": _int(checks=_NON_NEGATIVE),
        "reasoning_tokens": _int(checks=_NON_NEGATIVE),
        "input_text_tokens": _int(checks=_NON_NEGATIVE),
        "input_audio_tokens": _int(checks=_NON_NEGATIVE),
        "input_image_tokens": _int(checks=_NON_NEGATIVE),
        "output_text_tokens": _int(checks=_NON_NEGATIVE),
        "output_audio_tokens": _int(checks=_NON_NEGATIVE),
        "output_image_tokens": _int(checks=_NON_NEGATIVE),
        "accepted_prediction_tokens": _int(checks=_NON_NEGATIVE),
        "rejected_prediction_tokens": _int(checks=_NON_NEGATIVE),
        "usage_missing": _bool(nullable=False),

        # --- 請求參數 ---
        "tool_choice": _string(),
        "parallel_tool_calls": _bool(),
        "store": _bool(),
        "prompt_cache_key": _string(),
        "temperature": Column("Float64", nullable=True, checks=[Check.ge(0)]),
        "tool_count": _int(checks=_NON_NEGATIVE),
        "tool_types": _string(),
        "instructions_length": _int(checks=_NON_NEGATIVE),
        "reasoning_effort": _string(),
        "reasoning_summary": _string(),
        "reasoning_context": _string(),
        "text_verbosity": _string(),
        "text_format_name": _string(),
        "client_metadata_keys": _string(),
        "max_completion_tokens": _int(checks=_NON_NEGATIVE),
        "max_tokens": _int(checks=_NON_NEGATIVE),
        "include_options": _string(),
        "request_stream_option": _bool(),

        # --- 回應 ---
        "sse_bytes": _int(checks=_NON_NEGATIVE),
        "content_type": _string(),
        "truncated": _bool(),
        "finish_reasons": _string(),
        "error_type": _string(),
        "error_code": _string(),
        "error_param": _string(),

        # --- 影像請求 ---
        "image_model": _string(),
        "image_n": _int(checks=_NON_NEGATIVE),
        "image_quality": _string(),
        "image_size": _string(),

        # --- 內容長度（原文不進表）---
        "prompt_len": _int(nullable=False, checks=_NON_NEGATIVE),
        "assistant_response_len": _int(nullable=False, checks=_NON_NEGATIVE),
        "memory_len": _int(nullable=False, checks=_NON_NEGATIVE),

        # --- 訊息 ---
        "messages_format": _string(nullable=False),
        "message_count": _int(nullable=False, checks=_NON_NEGATIVE),
        "user_message_count": _int(nullable=False, checks=_NON_NEGATIVE),
        "assistant_message_count": _int(nullable=False, checks=_NON_NEGATIVE),

        # --- 其他 ---
        "noise_category": _string(nullable=False),

        # --- 血緣 ---
        "pipeline_version": _string(nullable=False),
        "ingested_at": Column(UTC_DTYPE, nullable=False),
    },
    checks=[
        Check(_wall_clock_delta_is_8h, name="ts_taipei_is_utc_plus_8"),
        Check(_date_matches_ts_taipei, name="date_taipei_derived_from_ts_taipei"),
        Check(_client_type_matches_thread, name="client_type_matches_thread_id"),
    ],
    strict=True,      # 多出未宣告的欄位也算違約
    ordered=False,
    name="RequestSchema",
)


# ---------------------------------------------------------------------------
# 警告層（不中止）
# ---------------------------------------------------------------------------
def run_warning_checks(frame: pd.DataFrame) -> list[str]:
    """回傳警告訊息清單，同時寫入 log。不 raise。"""
    warnings: list[str] = []

    # 檢查 9：log_lag_ms 中位數漂移。
    # 它不是效能指標（與 latency_ms 相關係數僅 0.007），留著是當上游架構的
    # 哨兵：日誌落地端的固定延遲若改變，代表匯出流程被動過，時間戳語意可能已變。
    lag = frame["log_lag_ms"].dropna()
    if len(lag):
        median = float(lag.astype("float64").median())
        drift = median - LOG_LAG_EXPECTED_MEDIAN_MS
        if abs(drift) > LOG_LAG_TOLERANCE_MS:
            warnings.append(
                f"log_lag_ms 中位數 {median:,.0f}ms 偏離預期 "
                f"{LOG_LAG_EXPECTED_MEDIAN_MS:,}ms 達 {drift:+,.0f}ms"
                f"（容許 ±{LOG_LAG_TOLERANCE_MS:,}ms）：上游時間戳行為可能已改變，須人工確認"
            )

    # 檢查 10：未見過的類別值。
    for column, known in (("endpoint", KNOWN_ENDPOINTS),
                          ("provider", KNOWN_PROVIDERS),
                          ("originator", KNOWN_ORIGINATORS)):
        seen = set(frame[column].dropna().unique()) - set(known)
        if seen:
            warnings.append(
                f"{column} 出現未見過的值 {sorted(seen)}"
                "：可能是上游新增功能，也可能是解析錯位，須人工確認"
            )

    for message in warnings:
        logger.warning("[驗證警告] %s", message)
    return warnings


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def load_dataset() -> pd.DataFrame:
    """讀回 L1 輸出。分區鍵回讀是 category（值存在目錄名裡），統一成 string。"""
    frame = pd.read_parquet(config.DATA_REQUEST)
    frame["date_taipei"] = frame["date_taipei"].astype("string")
    return frame


def check_columns_match(columns: list[str]) -> None:
    """schema 與 extract.COLUMNS 的欄位集合必須一致。

    不做會怎樣：兩邊各自維護，新增欄位時只改一邊，schema 就會安靜地
    不再涵蓋新欄位——契約看似通過，實際上有欄位從未被驗證過。
    """
    from src import extract

    declared = set(REQUEST_SCHEMA.columns)
    produced = set(extract.COLUMNS)
    if declared != produced:
        raise ValueError(
            "schema 與 extract.COLUMNS 不一致：\n"
            f"  只在 schema：{sorted(declared - produced)}\n"
            f"  只在 extract：{sorted(produced - declared)}"
        )
    unknown = set(columns) - declared
    missing = declared - set(columns)
    if unknown or missing:
        raise ValueError(
            f"實際資料欄位與 schema 不一致：多出 {sorted(unknown)}、缺少 {sorted(missing)}"
        )


def describe_failures(exc: pa.errors.SchemaErrors) -> list[str]:
    """把 pandera 的失敗明細壓成「哪一欄、哪條檢查、幾筆、樣本」。"""
    cases = exc.failure_cases
    lines: list[str] = []
    group = cases.groupby(["column", "check"], dropna=False)
    for (column, check), part in group:
        sample = part["failure_case"].head(3).tolist()
        index = part["index"].head(3).tolist() if "index" in part else []
        lines.append(
            f"欄位 {column!r} 未通過檢查 {check!r}：{len(part)} 筆"
            f"，樣本值 {sample!r}，列號 {index!r}"
        )
    return lines


def validate(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """驗證一份 L1 輸出。失敗即 raise SchemaErrors。回傳 (資料, 警告清單)。"""
    check_columns_match(list(frame.columns))
    try:
        validated = REQUEST_SCHEMA.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        logger.error("Schema 驗證失敗，共 %d 項：", len(exc.failure_cases))
        for line in describe_failures(exc):
            logger.error("  %s", line)
        raise
    warnings = run_warning_checks(frame)
    logger.info(
        "Schema 驗證通過：%d 列 × %d 欄，警告 %d 項",
        len(validated), validated.shape[1], len(warnings),
    )
    return validated, warnings


def validate_dataset() -> tuple[pd.DataFrame, list[str]]:
    return validate(load_dataset())
