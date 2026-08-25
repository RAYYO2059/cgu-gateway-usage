"""lite L1 輸出的資料契約。

與 schema.py 同一套設計，但驗的是另一組資料，兩者不共用 schema 物件——
lite 與 clean 的欄位集合不同，硬要共用只會逼出一堆 nullable 例外，
最後兩邊都驗不到東西。

兩類檢查：
- LITE_REQUEST_SCHEMA：不符即 raise。用於「一定是錯」的情況。
- run_warning_checks()：只記 log。用於「可能是上游變了，需要人看一眼」。

底下每一條斷言都是在 120,520 筆全量 lite 資料上實測為零違反之後才寫進來的，
不是照著文件抄的。註解記的是「不成立的話代表什麼壞掉了」。

欄位清單刻意在這裡重新列一次，不從 extract_lite import。
若兩邊漂移，check_columns_match() 會抓到——schema 若由被驗證的程式推導出來，
就驗不到「dtype 對照表被改壞」這種錯。
"""

from __future__ import annotations

import logging
import re

import pandas as pd
import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema

from src import config

logger = logging.getLogger(__name__)

TAIPEI_DTYPE = pd.DatetimeTZDtype(tz=config.TIMEZONE)
UTC_DTYPE = pd.DatetimeTZDtype(tz="UTC")

# anonymous_user_id 的格式。實測 357 個 uid 全部符合。
UID_PATTERN = r"^user_[0-9a-f]{16}$"
_UID_RE = re.compile(UID_PATTERN)

# --- 校準常數 --------------------------------------------------------------
# log_lag_ms = ts_utc(received) - ts_created_utc(created)，實測中位數 -27,621ms
# （負值代表上游落地時間戳晚於 gateway 收件，與 clean 同向）。
#
# clean 那邊的對應值是 -20,000ms、窄帶 17.5~20.8 秒；lite 是另一條匯出流程，
# 窄帶落在約 27.6 秒，所以兩邊的常數不同，這是預期的，不要互相對齊。
#
# 這**不是**效能指標：與 latency_ms 的相關係數僅 0.06，|差值|≤1s 的只有 3.9%。
# 留著是當上游匯出流程的哨兵——這個中位數一旦漂移，代表落地流程被動過，
# 時間戳語意可能已經改變，而所有按日期切分的統計都建立在它之上。
LOG_LAG_EXPECTED_MEDIAN_MS = -27_600
LOG_LAG_TOLERANCE_MS = 5_000

# 已知的類別值。新值不是錯，但要有人確認語意，所以只警告。
KNOWN_ENDPOINTS = frozenset({
    "/v1/responses", "/v1/chat/completions", "/v1/embeddings",
    "/v1/audio/speech", "/v1/audio/transcriptions",
    "/v1/images/generations", "/v1/images/edits",
    "/v1/responses/input_tokens",
})
KNOWN_PROVIDERS = frozenset({"openai", "ollama"})
KNOWN_SCHEMA_VERSIONS = frozenset({"1.1-lite"})

# user_account 覆蓋率。實測 120,520/120,520 = 100%。
# 訂成警告而不是硬性斷言：覆蓋率掉下來是上游的事，不該讓整條 pipeline 停擺，
# 但它是學院歸屬的唯一來源，掉了就要立刻有人知道。
ACCOUNT_COVERAGE_EXPECTED = 1.0
ACCOUNT_COVERAGE_TOLERANCE = 0.01


# ---------------------------------------------------------------------------
# 跨欄位檢查
# ---------------------------------------------------------------------------
def _total_equals_parts(frame: pd.DataFrame) -> pd.Series:
    """不做會怎樣：total_tokens 是所有用量彙總的分母。實測 120,520/120,520
    滿足 total == prompt + completion；若上游哪天把 reasoning 或 cache_write
    也加進 total，這條會先炸，而不是等到成本估算多算一倍才有人發現。
    """
    parts = frame["prompt_tokens"].fillna(0) + frame["completion_tokens"].fillna(0)
    return frame["total_tokens"].fillna(0) == parts


def _cached_within_prompt(frame: pd.DataFrame) -> pd.Series:
    """不做會怎樣：成本估算把 prompt 拆成「未命中 + 快取命中」兩段計價，
    未命中段 = prompt - cached。cached 若大於 prompt，未命中段變負數，
    金額被無聲扣掉一塊。實測零違反。
    """
    return frame["cached_tokens"].fillna(0) <= frame["prompt_tokens"].fillna(0)


def _reasoning_within_completion(frame: pd.DataFrame) -> pd.Series:
    """不做會怎樣：reasoning_tokens 是 completion_tokens 的子集（實測零違反，
    且 total == prompt + completion 同時成立，兩者互為佐證）。若哪天它變成
    獨立計費的項目，把它當子集就會少算；這條會先擋下來。
    """
    return frame["reasoning_tokens"].fillna(0) <= frame["completion_tokens"].fillna(0)


def _date_matches_ts_taipei(frame: pd.DataFrame) -> pd.Series:
    """不做會怎樣：分區鍵若不是從 ts_taipei 推導（例如誤用來源資料夾名，
    那是 UTC 曆日），跨日界的請求會落在錯誤分區。實測全期有 10 筆請求在
    received/created/資料夾名三種口徑下會分到不同日期。
    """
    expected = frame["ts_taipei"].dt.strftime("%Y-%m-%d")
    return frame["date_taipei"].astype("string") == expected.astype("string")


def _uid_format(series: pd.Series) -> pd.Series:
    """不做會怎樣：anonymous_user_id 是 lite 唯一的人層級鍵。格式若混入
    別種樣態，代表上游換了匿名化方案，而跨批次的 uid 就不再可比——
    整個時間序列會在某一天悄悄斷掉。
    """
    return series.fillna("").map(lambda v: bool(_UID_RE.match(v)))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def _string(nullable: bool = True, **kwargs) -> Column:
    return Column("string", nullable=nullable, **kwargs)


def _int(nullable: bool = True, checks: list | None = None) -> Column:
    return Column("Int64", nullable=nullable, checks=checks)


_NON_NEGATIVE = [Check.ge(0, name="non_negative")]

LITE_REQUEST_SCHEMA = DataFrameSchema(
    {
        # 來源
        "source_path": _string(nullable=False),
        "source_file": _string(),
        "source_record_index": _int(),
        "date_dir": _string(),
        # request_id 唯一且非空：它是去重與跨批次比對的唯一鍵。
        "request_id": _string(nullable=False, unique=True),

        # 時間
        "ts_utc": Column(UTC_DTYPE, nullable=False),
        "ts_taipei": Column(TAIPEI_DTYPE, nullable=False),
        "date_taipei": _string(nullable=False),
        "hour_taipei": _int(checks=[Check.in_range(0, 23)]),
        "weekday_taipei": _int(checks=[Check.in_range(0, 6)]),
        "ts_created_utc": Column(UTC_DTYPE, nullable=True),
        "ts_created_taipei": Column(TAIPEI_DTYPE, nullable=True),
        "log_lag_ms": _int(),

        # 身分
        "anonymous_user_id": _string(
            nullable=False,
            checks=[Check(_uid_format, element_wise=False, name="uid_format")]),
        "user_account": _string(),

        # 請求
        "endpoint": _string(),
        "provider": _string(
            checks=[Check.isin(sorted(KNOWN_PROVIDERS), name="known_provider")]),
        "model_requested": _string(),
        "model_returned": _string(),
        "status_code": _int(),
        "stream": Column("boolean", nullable=True),
        "latency_ms": Column("Float64", nullable=True),
        "response_length": _int(),
        "thread_id": _string(),
        "conversation_id": _string(),
        "request_style": _string(),

        # 提示詞。刻意沒有 prompt_text 這一欄：明文不落地。
        "prompt_length": _int(checks=_NON_NEGATIVE),
        "prompt_text_len": _int(checks=_NON_NEGATIVE),
        "prompt_text_sha256": _string(),

        # 用量
        "prompt_tokens": _int(checks=_NON_NEGATIVE),
        "cached_tokens": _int(checks=_NON_NEGATIVE),
        "cache_write_tokens": _int(checks=_NON_NEGATIVE),
        "completion_tokens": _int(checks=_NON_NEGATIVE),
        "reasoning_tokens": _int(checks=_NON_NEGATIVE),
        "total_tokens": _int(checks=_NON_NEGATIVE),

        # 代理
        "tool_count": _int(checks=_NON_NEGATIVE),
        "agent_count": _int(checks=_NON_NEGATIVE),

        # 上游自述
        "schema_name": _string(),
        "schema_version": _string(),
        "cleaner_version": _string(),
        "anonymizer_version": _string(),
        "processed_at": _string(),

        # 本地
        "ingested_at": Column(UTC_DTYPE, nullable=False),
    },
    checks=[
        Check(_total_equals_parts, name="total_equals_prompt_plus_completion"),
        Check(_cached_within_prompt, name="cached_le_prompt"),
        Check(_reasoning_within_completion, name="reasoning_le_completion"),
        Check(_date_matches_ts_taipei, name="date_taipei_from_ts_taipei"),
    ],
    strict=True,
    name="lite_request",
)


# ---------------------------------------------------------------------------
# 分區完整度
# ---------------------------------------------------------------------------
HOURS_PER_DAY = 24


def partition_coverage(frame: pd.DataFrame) -> pd.DataFrame:
    """每個 date_taipei 分區涵蓋幾個不重複小時。不足 24 即標為 partial。

    為什麼首尾兩天必然不完整
    ------------------------
    台北是 UTC+8，所以 UTC 的某一天 D 換算成台北是 D 08:00 一路到 D+1 07:59。
    換句話說**每一個完整的台北日都要橫跨兩個 UTC 資料夾**才湊得滿 24 小時：
    早上 8 點之前那段來自前一個 UTC 日，之後那段來自當日。

    於是資料範圍的兩端必然是殘的：最早那個台北日只拿得到 08:00 之後的 16
    小時，最晚那個台北日只拿得到 08:00 之前的 8 小時。本批的
    ``date_taipei=2026-08-24`` 就是這樣來的——它整天的資料只有 UTC 08-23
    那個資料夾的尾端，不是資料出了問題。

    這張表同時抓得到三種不同的狀況，而它們的處置方式完全不同：

    - **時區邊界殘日**：只出現在頭尾，缺的小時是連續的一段。正常，
      但逐日趨勢要排除或標記，否則首尾兩天會看起來像用量暴跌。
    - **資料缺漏**：中間某天缺了小時。上游匯出漏檔，要回頭補。
    - **上游中斷**：中間某天缺的是連續的一段。服務當時掛了，
      那是事實而不是資料問題，但不能當成使用行為來解讀。

    「缺幾小時」本身分不出這三者，而且會製造大量假警報：一個流量低的日子
    深夜某個小時沒有任何請求，就會少一小時，那是使用行為不是資料問題。
    真正有訊息量的是**缺口的形狀**——

    - 零星、分散的缺口 → 那幾個小時沒人用，正常。
    - **連續**的一段缺口 → 那段時間沒有資料流進來，才值得查。
      貼著 0 點或 23 點的連續缺口尤其可疑，因為它通常延伸到相鄰的日子。

    所以表裡除了最早／最晚小時，另外給出最長連續缺口，警告也只對它升級。
    """
    hours = frame["ts_taipei"].dt.hour
    base = pd.DataFrame({"date_taipei": frame["date_taipei"].astype("string"),
                         "hour": hours})

    rows = []
    for day, part in base.groupby("date_taipei", sort=True):
        present = set(int(h) for h in part["hour"].unique())
        missing = sorted(set(range(HOURS_PER_DAY)) - present)
        runs = _consecutive_runs(missing)
        longest = max((len(r) for r in runs), default=0)
        rows.append({
            "date_taipei": day,
            "n_requests": len(part),
            "n_distinct_hours": len(present),
            "最早小時": int(part["hour"].min()),
            "最晚小時": int(part["hour"].max()),
            "is_partial": len(present) < HOURS_PER_DAY,
            "缺幾小時": HOURS_PER_DAY - len(present),
            "最長連續缺口": longest,
            "缺的小時": _format_runs(runs),
        })
    return pd.DataFrame(rows).sort_values("date_taipei").reset_index(drop=True)


def _consecutive_runs(values: list[int]) -> list[list[int]]:
    """把 [0,1,2,5,7,8] 切成 [[0,1,2],[5],[7,8]]。"""
    runs: list[list[int]] = []
    for value in values:
        if runs and value == runs[-1][-1] + 1:
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


def _format_runs(runs: list[list[int]]) -> str:
    if not runs:
        return ""
    return ",".join(str(r[0]) if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in runs)


# 連續缺口達幾小時才算「值得查」。低於這個數的零星缺口是「那小時沒人用」。
GAP_ALERT_HOURS = 3


def format_partition_coverage(table: pd.DataFrame) -> str:
    lines = [f"  {'date_taipei':<13}{'n_requests':>11}{'小時':>6}{'最早':>5}"
             f"{'最晚':>5}{'缺':>4}{'最長缺口':>9}  {'缺的小時':<14}狀態"]
    for _, r in table.iterrows():
        if not r["is_partial"]:
            state = ""
        elif r["最長連續缺口"] >= GAP_ALERT_HOURS:
            state = "partial ★連續缺口"
        else:
            state = "partial（零星）"
        lines.append(
            f"  {r['date_taipei']:<13}{int(r['n_requests']):>11,}"
            f"{int(r['n_distinct_hours']):>6}{int(r['最早小時']):>5}"
            f"{int(r['最晚小時']):>5}{int(r['缺幾小時']):>4}"
            f"{int(r['最長連續缺口']):>9}  {r['缺的小時']:<14}{state}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 警告類檢查
# ---------------------------------------------------------------------------
def run_warning_checks(frame: pd.DataFrame) -> list[str]:
    """回傳警告訊息清單，同時寫入 log。不 raise。"""
    warnings: list[str] = []

    # 檢查 1：log_lag_ms 中位數漂移（上游落地流程的哨兵）。
    lag = frame["log_lag_ms"].dropna()
    if len(lag):
        median = float(lag.astype("float64").median())
        drift = median - LOG_LAG_EXPECTED_MEDIAN_MS
        if abs(drift) > LOG_LAG_TOLERANCE_MS:
            warnings.append(
                f"log_lag_ms 中位數 {median:,.0f}ms 偏離預期 "
                f"{LOG_LAG_EXPECTED_MEDIAN_MS:,}ms 達 {drift:+,.0f}ms"
                f"（容許 ±{LOG_LAG_TOLERANCE_MS:,}ms）："
                "上游匯出流程可能已改變，時間戳語意須人工確認"
            )

    # 檢查 2：user_account 覆蓋率。它是學院歸屬的唯一來源。
    if len(frame):
        coverage = float(frame["user_account"].notna().mean())
        if abs(coverage - ACCOUNT_COVERAGE_EXPECTED) > ACCOUNT_COVERAGE_TOLERANCE:
            warnings.append(
                f"user_account 覆蓋率 {coverage:.2%} 偏離預期 "
                f"{ACCOUNT_COVERAGE_EXPECTED:.0%}："
                "學院／身分別歸屬會出現無法解析的列，須人工確認"
            )

    # 檢查 3：未見過的類別值。
    for column, known in (("endpoint", KNOWN_ENDPOINTS),
                          ("provider", KNOWN_PROVIDERS),
                          ("schema_version", KNOWN_SCHEMA_VERSIONS)):
        seen = set(frame[column].dropna().unique()) - set(known)
        if seen:
            warnings.append(
                f"{column} 出現未見過的值 {sorted(seen)}"
                "：可能是上游新增功能，也可能是解析錯位，須人工確認"
            )

    # 檢查 4：uid → user_account 必須單值。
    # 這是 identity_lite 的核心前提（見該模組 docstring）：uid 是人層級鍵，
    # account 只是屬性。一旦某個 uid 掛到兩個帳號，「這個人屬於哪個學院」
    # 就變成有兩個答案，而下游不會察覺，只會取到其中一個。
    pairs = frame[["anonymous_user_id", "user_account"]].dropna().drop_duplicates()
    multi = pairs.groupby("anonymous_user_id").size()
    offenders = multi[multi > 1]
    if len(offenders):
        warnings.append(
            f"有 {len(offenders)} 個 anonymous_user_id 對應到多個 user_account"
            f"（最多 {int(offenders.max())} 個）：identity_lite 假設這是單值的，"
            "學院歸屬會變成不確定，須人工確認"
        )

    # 檢查 5：分區完整度。頭尾殘日是時區邊界的必然結果、不是錯誤，
    # 所以這裡只警告；但中間出現 partial 就代表上游漏了東西或當時中斷過。
    coverage = partition_coverage(frame)
    partial = coverage[coverage["is_partial"]]
    if len(partial):
        edges = {coverage["date_taipei"].iloc[0], coverage["date_taipei"].iloc[-1]}
        # 只對「連續缺口」升級。零星缺一兩個小時是低流量時段沒人用，
        # 全部一視同仁的話，24 個分區會有 21 個在喊，警告就沒人看了。
        notable = partial[(partial["最長連續缺口"] >= GAP_ALERT_HOURS)
                          & ~partial["date_taipei"].isin(edges)]
        warnings.append(
            f"{len(partial)} 個分區不足 24 小時（頭尾兩天是 UTC+8 邊界的必然結果，"
            f"逐日趨勢須排除或標記）"
            + (f"；★ {len(notable)} 個分區有 {GAP_ALERT_HOURS} 小時以上的連續缺口："
               + "、".join(f"{r['date_taipei']}（缺 {r['缺的小時']} 時）"
                           for _, r in notable.iterrows())
               + "，須確認是上游漏檔還是服務中斷"
               if len(notable) else "；無 3 小時以上的連續缺口")
        )

    for message in warnings:
        logger.warning("[lite 驗證警告] %s", message)
    return warnings


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def check_columns_match(columns: list[str]) -> None:
    """schema 與 extract_lite.COLUMNS 的欄位集合必須一致。"""
    from src import extract_lite

    declared = set(LITE_REQUEST_SCHEMA.columns)
    produced = set(extract_lite.COLUMNS)
    if declared != produced:
        raise ValueError(
            "schema_lite 與 extract_lite.COLUMNS 不一致：\n"
            f"  只在 schema_lite：{sorted(declared - produced)}\n"
            f"  只在 extract_lite：{sorted(produced - declared)}"
        )
    unknown = set(columns) - declared
    missing = declared - set(columns)
    if unknown or missing:
        raise ValueError(
            f"實際資料欄位與 schema_lite 不一致：多出 {sorted(unknown)}、"
            f"缺少 {sorted(missing)}"
        )
    if "prompt_text" in columns:
        raise ValueError(
            "lite 輸出出現 prompt_text 欄位。明文提示詞不得落地，"
            "只應保留 prompt_text_len 與 prompt_text_sha256。"
        )


def describe_failures(exc: pa.errors.SchemaErrors) -> list[str]:
    cases = exc.failure_cases
    lines: list[str] = []
    for (column, check), part in cases.groupby(["column", "check"], dropna=False):
        sample = part["failure_case"].head(3).tolist()
        index = part["index"].head(3).tolist() if "index" in part else []
        lines.append(
            f"欄位 {column!r} 未通過檢查 {check!r}：{len(part)} 筆"
            f"，樣本值 {sample!r}，列號 {index!r}"
        )
    return lines


def validate(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """驗證一份 lite L1 輸出。失敗即 raise SchemaErrors。回傳 (資料, 警告清單)。"""
    check_columns_match(list(frame.columns))
    try:
        validated = LITE_REQUEST_SCHEMA.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        logger.error("lite schema 驗證失敗，共 %d 項：", len(exc.failure_cases))
        for line in describe_failures(exc):
            logger.error("  %s", line)
        raise
    warnings = run_warning_checks(frame)
    logger.info("lite schema 驗證通過：%d 列 × %d 欄，警告 %d 項",
                len(validated), validated.shape[1], len(warnings))
    return validated, warnings


def validate_dataset() -> tuple[pd.DataFrame, list[str]]:
    from src import extract_lite

    return validate(extract_lite.load_dataset())


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    frame, warnings = validate_dataset()
    print()
    print("分區完整度")
    print(format_partition_coverage(partition_coverage(frame)))
    return 1 if warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
