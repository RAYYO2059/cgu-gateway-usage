"""成本估算：把一筆請求的 token 用量換算成金額。

規則已在 lite 資料上驗證過，這裡只是實作，不重新推導：

1. ``provider == 'ollama'`` 是本地模型，不計價 → ``unpriced_local``。
2. 其餘 provider 走 token 兩段計價（快取命中的 prompt 另有費率）::

       cost = (prompt_tokens - cached_tokens) * 輸入價
            + cached_tokens                   * 快取價
            + completion_tokens               * 輸出價

   ``cached_tokens`` 為 null 視為 0。openai 子集裡有 4,784 筆是 null，
   涉及 13,512,118 個 prompt token，相對 4,266,072,862 的總量可忽略。
3. 非 token 計價端點（語音、圖片）→ ``unpriced_non_token``。
   這些端點**還是會回報 token 數**（images/generations 合計 69 萬），
   不擋掉的話會被文字費率算進去，錯得很像對的。
4. 找不到價目的 model_family → ``unpriced_no_table``。

另外 2026-08-18、08-19 兩天標 ``confidence = 'low'``：那兩天有 1,596 筆
2xx 請求 token 記成 0（另有 2 筆 0 token 落在 /v1/audio/transcriptions，
那是該端點的正常值，不算在內）。

日期一律取**台北曆日**，與 ``extract_lite`` 的分區鍵 ``date_taipei``、
以及 clean 的 ``date_taipei`` 對齊——全專案只有一種「哪一天」。
先前這裡用的是 ``created_at`` 的 UTC 曆日，換過來之後被標記的筆數從
5,925 變成 **5,593**（= 分區表的 08-18 的 2,579 + 08-19 的 3,014），
而 1,596 這個關鍵數字在兩種口徑下都成立，所以換口徑不影響已驗證的結論，
只是讓被標記的範圍不再混用兩套曆日。

價目表在 ``ref/pricing_table.csv``，單位是「每 1,000 token 的價格」。
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from src import config
# 版本後綴的剝除規則直接沿用 extract 的定義，不另外抄一份：
# 抄一份的話兩邊遲早會漂移，而漂移的症狀是「某個模型突然查不到價目」，
# 不會有人立刻聯想到是正規化規則不一致。
from src.extract import _MODEL_DATE_SUFFIX as MODEL_DATE_SUFFIX

logger = logging.getLogger(__name__)

PRICING_TABLE_PATH = config.REF_DIR / "pricing_table.csv"

PRICING_COLUMNS = (
    "model_family", "effective_date",
    "input_price_per_1k", "cached_input_price_per_1k", "output_price_per_1k",
    "source_url", "fetched_at",
)

# pricing_status 的四個值。
STATUS_PRICED = "priced"
STATUS_UNPRICED_LOCAL = "unpriced_local"
STATUS_UNPRICED_NON_TOKEN = "unpriced_non_token"
STATUS_UNPRICED_NO_TABLE = "unpriced_no_table"

CONFIDENCE_NORMAL = "normal"
CONFIDENCE_LOW = "low"

LOCAL_PROVIDERS = frozenset({"ollama"})

# 不以 token 計價的端點。語音按秒／字元、圖片按張計價。
NON_TOKEN_ENDPOINTS = frozenset({
    "/v1/audio/speech",
    "/v1/audio/transcriptions",
    "/v1/images/generations",
    "/v1/images/edits",
})

# token 記錄不可信的日期（**台北曆日**，與 date_taipei 分區鍵同一套曆日）。
LOW_CONFIDENCE_DATES = frozenset({date(2026, 8, 18), date(2026, 8, 19)})

# 台北時區。只在沒有現成 date_taipei/ts_taipei 欄位時才需要自己換算。
_TAIPEI_OFFSET = timedelta(hours=8)

# 同一個語意在兩套 schema 裡的欄位名：lite_index 用點號分層，
# 專案自己的 01_request parquet 是攤平的。先找攤平名再找點號名，
# 這樣同一個函式在兩邊都能跑，等 lite 欄位併進主管線也不用改。
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "provider": ("provider", "request.provider"),
    "endpoint": ("endpoint", "request.endpoint"),
    "model_returned": ("model_returned", "response.model_returned"),
    "prompt_tokens": ("prompt_tokens", "usage.prompt_tokens"),
    "cached_tokens": ("cached_tokens", "usage.cached_tokens"),
    "completion_tokens": ("completion_tokens", "usage.completion_tokens"),
    # estimate_cost() 本身用不到，但驗證腳本要靠它切「成功的請求」，
    # 放在同一份對照表裡才不會兩邊各維護一套欄位名。
    "status_code": ("status_code", "response.status_code"),
    # 台北曆日。優先用現成的欄位，兩者都沒有才退回從 UTC 時間戳自己換算。
    "date_taipei": ("date_taipei",),
    "ts_taipei": ("ts_taipei",),
    "ts_utc": ("ts_utc", "received_at", "ts_created_utc", "created_at"),
}


# ---------------------------------------------------------------------------
# 取值與轉型
# ---------------------------------------------------------------------------
def _field(row: Mapping[str, Any], name: str) -> Any:
    for key in _FIELD_ALIASES[name]:
        if key in row:
            value = row[key]
            if value is not None and value == value:  # NaN != NaN
                return value
    return None


def _as_int(value: Any) -> int:
    """token 欄位轉整數；null／NaN／空字串一律 0。

    parquet 裡 cached_tokens 是 double（因為有 null），不轉會讓
    「int - float」的結果變成 float，金額再乘下去就開始出現浮點尾巴。
    """
    if value is None or value != value:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_price(value: Any) -> float | None:
    if value is None or value != value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_date(value: Any) -> date | None:
    """把時間戳轉成 UTC 曆日。無法解析回 None。"""
    if value is None or value != value:
        return None
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, date):
        return value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    return stamp.date()


def taipei_date(row: Mapping[str, Any]) -> date | None:
    """這筆請求算哪一個**台北曆日**。無法判定回 None。

    優先序刻意是「現成欄位優先、自己換算最後」：
    ``date_taipei`` 是 extract_lite 寫進分區鍵的那個值，用它才保證與分區、
    與所有按日切分的統計完全一致；退回自己換算只是為了讓這個函式在還沒
    進過 L1 的原始 dict 上也能跑。
    """
    explicit = _field(row, "date_taipei")
    if explicit is not None:
        parsed = _as_date(explicit)
        if parsed is not None:
            return parsed

    stamp = _field(row, "ts_taipei")
    if stamp is not None:
        parsed = _as_date(stamp)
        if parsed is not None:
            return parsed

    # 最後手段：由 UTC 時間戳換算。注意不能直接取 UTC 曆日——那正是先前
    # 混用兩套曆日的來源。
    raw = _field(row, "ts_utc")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        stamp_utc = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            stamp_utc = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp_utc.tzinfo is None:
            stamp_utc = stamp_utc.replace(tzinfo=timezone.utc)
    return (stamp_utc.astimezone(timezone.utc) + _TAIPEI_OFFSET).date()


def model_family(model_returned: Any) -> str | None:
    """去掉版本日期後綴，例如 gpt-5.4-mini-2026-03-17 → gpt-5.4-mini。"""
    if model_returned is None or model_returned != model_returned:
        return None
    text = str(model_returned).strip()
    return MODEL_DATE_SUFFIX.sub("", text) if text else None


# ---------------------------------------------------------------------------
# 價目表
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PriceRow:
    model_family: str
    effective_date: date | None  # None = 從最早開始適用
    input_per_1k: float | None
    cached_input_per_1k: float | None
    output_per_1k: float | None
    source_url: str = ""
    fetched_at: str = ""
    line_no: int = 0

    @property
    def has_any_price(self) -> bool:
        """至少填了輸入價，這一列才算開始可用。

        「夠不夠用」要看該筆請求實際用到哪幾段——embeddings 沒有輸出 token，
        硬要求輸出價的話 text-embedding-* 永遠不會變成 priced，除非有人在
        輸出價欄填一個其實不存在的 0。缺哪一段就擋哪一段，見 estimate_cost。
        """
        return self.input_per_1k is not None


class PricingTable:
    """model_family → 依生效日排序的價目列。"""

    def __init__(self, rows: Iterable[PriceRow]) -> None:
        self._by_family: dict[str, list[PriceRow]] = {}
        for row in rows:
            self._by_family.setdefault(row.model_family, []).append(row)
        for group in self._by_family.values():
            # None（未指定生效日）排最前面，代表「一直都適用」。
            group.sort(key=lambda r: (r.effective_date is not None, r.effective_date or date.min))

    def __len__(self) -> int:
        return sum(len(g) for g in self._by_family.values())

    @property
    def families(self) -> list[str]:
        return sorted(self._by_family)

    @property
    def priced_families(self) -> list[str]:
        return sorted(f for f, g in self._by_family.items()
                      if any(r.has_any_price for r in g))

    def lookup(self, family: str | None, on_date: date | None) -> PriceRow | None:
        """取某個 family 在某日適用的價目列。

        on_date 為 None（請求沒有可解析的時間）時只有在該 family 只有一列
        價目時才回答；有多列就是不知道該用哪一段，回 None 讓上層標成未定價。
        """
        if not family:
            return None
        group = self._by_family.get(family)
        if not group:
            return None
        if on_date is None:
            return group[0] if len(group) == 1 else None
        applicable = [r for r in group
                      if r.effective_date is None or r.effective_date <= on_date]
        return applicable[-1] if applicable else None

    @classmethod
    def from_csv(cls, path: Path | None = None) -> "PricingTable":
        path = path or PRICING_TABLE_PATH
        if not path.is_file():
            raise FileNotFoundError(
                f"找不到價目表 {path}。\n"
                f"這張表是人工維護的，欄位為 {', '.join(PRICING_COLUMNS)}。"
            )
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            missing = [c for c in PRICING_COLUMNS if c not in header]
            if missing:
                raise ValueError(
                    f"{path} 缺少欄位 {missing}；應為 {', '.join(PRICING_COLUMNS)}")
            rows = []
            for line_no, raw in enumerate(reader, start=2):
                family = (raw.get("model_family") or "").strip()
                if not family:
                    continue
                rows.append(PriceRow(
                    model_family=family,
                    effective_date=_as_date(raw.get("effective_date")),
                    input_per_1k=_as_price(raw.get("input_price_per_1k")),
                    cached_input_per_1k=_as_price(raw.get("cached_input_price_per_1k")),
                    output_per_1k=_as_price(raw.get("output_price_per_1k")),
                    source_url=(raw.get("source_url") or "").strip(),
                    fetched_at=(raw.get("fetched_at") or "").strip(),
                    line_no=line_no,
                ))
        return cls(rows)


# ---------------------------------------------------------------------------
# 估算
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CostEstimate:
    """一筆請求的估算結果。

    中間值（uncached/cached/completion 與三段金額）一併回傳：驗證拆分公式時
    只看總額看不出是哪一段錯了，而未定價時總額一律是 None，更沒東西可看。
    """

    pricing_status: str
    confidence: str
    model_family: str | None
    prompt_tokens: int
    cached_tokens: int
    uncached_prompt_tokens: int
    completion_tokens: int
    input_cost: float | None = None
    cached_cost: float | None = None
    output_cost: float | None = None
    cost: float | None = None
    price_effective_date: date | None = None
    note: str = ""


def estimate_cost(
    row: Mapping[str, Any],
    pricing_table: PricingTable,
) -> CostEstimate:
    """估算單筆請求的成本。

    row 可以是 dict 或 pandas Series，欄位名支援 lite_index 的點號寫法與
    專案 parquet 的攤平寫法（見 _FIELD_ALIASES）。
    """
    provider = _field(row, "provider")
    endpoint = _field(row, "endpoint")
    family = model_family(_field(row, "model_returned"))
    on_date = taipei_date(row)

    prompt_tokens = _as_int(_field(row, "prompt_tokens"))
    cached_tokens = _as_int(_field(row, "cached_tokens"))
    completion_tokens = _as_int(_field(row, "completion_tokens"))

    note = ""
    # cached 不該大於 prompt；真的發生時夾住，否則未命中段會變負數，
    # 讓總額被無聲地扣掉一塊。目前的 lite 資料沒有這種列。
    if cached_tokens > prompt_tokens:
        note = f"cached_tokens({cached_tokens}) > prompt_tokens({prompt_tokens})，已夾住"
        cached_tokens = prompt_tokens
    uncached = prompt_tokens - cached_tokens

    confidence = CONFIDENCE_LOW if on_date in LOW_CONFIDENCE_DATES else CONFIDENCE_NORMAL

    def unpriced(status: str, extra: str = "") -> CostEstimate:
        return CostEstimate(
            pricing_status=status,
            confidence=confidence,
            model_family=family,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            uncached_prompt_tokens=uncached,
            completion_tokens=completion_tokens,
            note="；".join(p for p in (note, extra) if p),
        )

    # 規則順序即判斷順序：本地模型不論打哪個端點都不計價。
    if str(provider or "").lower() in LOCAL_PROVIDERS:
        return unpriced(STATUS_UNPRICED_LOCAL)

    if endpoint in NON_TOKEN_ENDPOINTS:
        return unpriced(STATUS_UNPRICED_NON_TOKEN)

    price = pricing_table.lookup(family, on_date)
    if price is None:
        if not family:
            return unpriced(STATUS_UNPRICED_NO_TABLE, "model_returned 為空")
        if on_date is None:
            return unpriced(STATUS_UNPRICED_NO_TABLE, "請求日期無法解析，無法選生效價目")
        return unpriced(STATUS_UNPRICED_NO_TABLE, f"價目表沒有 {family}")
    # 缺價格只擋真正用得到的那幾段：少填一段就用 0 或用輸入價頂替，
    # 會算出一個偏低但看起來正常的金額，比直接標 unpriced_no_table 危險得多。
    if price.input_per_1k is None:
        return unpriced(STATUS_UNPRICED_NO_TABLE, f"{family} 的輸入價尚未填")
    if completion_tokens > 0 and price.output_per_1k is None:
        return unpriced(STATUS_UNPRICED_NO_TABLE, f"{family} 有輸出 token 但輸出價尚未填")
    if cached_tokens > 0 and price.cached_input_per_1k is None:
        return unpriced(STATUS_UNPRICED_NO_TABLE, f"{family} 有快取命中但快取價尚未填")

    input_cost = uncached / 1000 * price.input_per_1k
    cached_cost = cached_tokens / 1000 * (price.cached_input_per_1k or 0.0)
    output_cost = completion_tokens / 1000 * (price.output_per_1k or 0.0)

    return CostEstimate(
        pricing_status=STATUS_PRICED,
        confidence=confidence,
        model_family=family,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        uncached_prompt_tokens=uncached,
        completion_tokens=completion_tokens,
        input_cost=input_cost,
        cached_cost=cached_cost,
        output_cost=output_cost,
        cost=input_cost + cached_cost + output_cost,
        price_effective_date=price.effective_date,
        note=note,
    )


def estimate_frame(frame, pricing_table: PricingTable):
    """對整個 DataFrame 逐列估算，回傳同長度的結果 DataFrame。

    刻意逐列呼叫 estimate_cost 而不另外寫一份向量化版本：向量化會快個幾十倍，
    但驗證報告就不再是在驗證真正上線的那個函式了。12 萬列跑幾秒，還不值得換。
    """
    import pandas as pd

    records = [vars(estimate_cost(row, pricing_table))
               for row in frame.to_dict("records")]
    return pd.DataFrame.from_records(records, index=frame.index)
