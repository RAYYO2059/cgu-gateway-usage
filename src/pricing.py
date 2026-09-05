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


這份估算算不出來的三件事
------------------------
**這是牌價等值成本，不是實際帳單。** 底下三項都會讓實際金額與本估算有系統性
差距，而且都無法從 lite 資料判斷——不是精度問題，是資料裡沒有那個欄位。

1. **service_tier 一律假設 Standard。** 官方分四級：Batch 半價、Flex 半價、
   Fast 兩倍、Standard 預設。lite schema 沒有這個欄位。若實際大量使用 Batch，
   **本估算會高估一倍**；若使用 Fast 則會低估一半。這是四項裡影響最大的一項。

2. **區域處理加價未計。** 2026-03-05 之後發布且符合資料落地資格的模型，走區域
   端點會加收 10%。無法從資料判斷 gateway 走的是全球還是區域端點。
   **待向單位確認**；若成立，gpt-5.4 之後的模型金額需上調 10%。

3. **促銷價有時效。** gpt-5.6-sol 自 2026-08-22 起的價格是官方標明的促銷價，
   至少維持到 2026-11-21。該日之後若有人用更新的價目表重算同一批資料，數字
   會變。價目列的 expires_at 欄記著這件事。

4. **本估算的可重現性性質與本專案其他指標不同。** 其他所有指標都是**資料的
   函數**：資料不變，重跑必然得到一樣的數字，這也是 --publish 冪等的基礎。
   成本估算不是——它是「資料 × 外部價目 × 取價時間」的函數。

   所以「同一批資料在不同時間重算得到不同數字」在這裡**不是 bug**，而是這個
   指標的性質。要重現一份成本數字，必須同時固定資料與 ref/pricing_table.csv
   的內容；可重現性綁在該表的 fetched_at 上，不綁在資料上。

   引用本估算的文件請一併註明取價日期，否則那個數字在幾個月後會對不起來，
   而且沒有人查得出為什麼。
"""

from __future__ import annotations

import csv
import io
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
    "model_family", "effective_date", "context_tier",
    "input_price_per_1k", "input_confidence",
    "cached_input_price_per_1k", "cached_input_confidence",
    "cache_write_price_per_1k", "cache_write_confidence",
    "output_price_per_1k", "output_confidence",
    "expires_at", "note", "source_url", "fetched_at",
)

# 長上下文級距的門檻。超過這個 prompt_tokens 的請求適用另一組（約兩倍）價格。
LONG_CONTEXT_THRESHOLD = 272_000
TIER_SHORT = "short"
TIER_LONG = "long"

# price_confidence：價目本身有多可靠。**與 pricing_status 是兩件事**——
# 後者回答「這筆能不能計價」，前者回答「用來計價的那個數字有多硬」。
# 混成一欄之後，「有多少筆算不出來」與「有多少筆算得出來但價格存疑」
# 就再也分不開。
#
# **粒度是「每個價位」不是「每一列」。** 一列價目有四個價位，它們的來源
# 可以不同：8/22 前的 gpt-5.6-sol 只有快取寫入是推估的，輸入／快取讀／
# 輸出都是官方明列。整列標 inferred 會讓彙總說「一半的金額建立在推估價
# 上」，而實際只有 13.6%——那個數字會讓人質疑整份估算，而質疑是我們
# 自己製造的。彙總請用 confidence_breakdown()，按段金額加權。
CONF_CONFIRMED = "confirmed"      # 官方頁面明列、對應無歧義
CONF_INFERRED = "inferred"        # 由規律推得（8/22 前 Sol 的快取寫入）
CONF_PROVISIONAL = "provisional"  # 對應到哪一列有歧義（gpt-5.2-chat-latest）
CONF_PROMOTIONAL = "promotional"  # 官方列出但有時效，之後重算會變
CONF_UNPRICED = "unpriced"        # 沒有計價，談不上可靠度

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
    "cache_write_tokens": ("cache_write_tokens", "usage.cache_write_tokens"),
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
def _is_null(value: Any) -> bool:
    """None / float('nan') / pandas 的 NA 一律視為空。

    不能只寫 ``value != value``：那對 float NaN 成立，但 pandas 的 pd.NA
    在布林脈絡下會直接 raise（"boolean value of NA is ambiguous"）。
    L1 lite 的字串欄位用的正是 string dtype，缺值就是 pd.NA，所以這條路徑
    一定會被走到。
    """
    if value is None:
        return True
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        # pd.NA 之類在布林脈絡下會炸的哨兵值，本身就代表「沒有值」。
        return True


def _field(row: Mapping[str, Any], name: str) -> Any:
    for key in _FIELD_ALIASES[name]:
        if key in row:
            value = row[key]
            if not _is_null(value):
                return value
    return None


def _as_int(value: Any) -> int:
    """token 欄位轉整數；null／NaN／空字串一律 0。

    parquet 裡 cached_tokens 是 double（因為有 null），不轉會讓
    「int - float」的結果變成 float，金額再乘下去就開始出現浮點尾巴。
    """
    if _is_null(value):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_price(value: Any) -> float | None:
    if _is_null(value):
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
    if _is_null(value):
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


# 有價格就必須標來源。空白不預設成 confirmed——那是往危險方向失效：
# 補價格的人忘了填 confidence，那個數字就靜默變成「官方明列」，
# 而這一欄存在的唯一理由就是防這件事。價目表還有 47 列待填，
# 這條規則會在第一次漏填時就擋下來。
#
# 判斷與先前反轉抑制預設值相同：預設值該往安全的方向失效。過度標示看得見
# （有人會來問「這為什麼是 inferred」），靜默標成 confirmed 看不見。
VALID_CONFIDENCE = frozenset({
    CONF_CONFIRMED, CONF_INFERRED, CONF_PROVISIONAL, CONF_PROMOTIONAL,
})


def _conf(value: Any, *, price: float | None, field: str,
          family: str, line_no: int) -> str:
    """讀某個價位的 confidence 欄。有價格卻空白即 raise。"""
    text = "" if _is_null(value) else str(value).strip()
    if price is None:
        # 沒有價格的價位不會被用到，confidence 是什麼都無所謂。
        return text or CONF_UNPRICED
    if not text:
        raise ValueError(
            f"{PRICING_TABLE_PATH} 第 {line_no} 行（{family}）："
            f"{field} 有價格 {price} 但對應的 confidence 欄空白。\n"
            "有價格就必須明確標示來源，不會預設成 confirmed——"
            f"請填 {sorted(VALID_CONFIDENCE)} 之一：\n"
            "  confirmed   官方頁面明列、對應無歧義\n"
            "  inferred    由規律推得（例：5.6 家族快取寫入為輸入的 1.25 倍）\n"
            "  provisional 對應到哪一列有歧義\n"
            "  promotional 官方列出但有時效"
        )
    if text not in VALID_CONFIDENCE:
        raise ValueError(
            f"{PRICING_TABLE_PATH} 第 {line_no} 行（{family}）："
            f"{field} 的 confidence {text!r} 不是合法值，"
            f"須為 {sorted(VALID_CONFIDENCE)} 之一"
        )
    return text


def model_family(model_returned: Any) -> str | None:
    """去掉版本日期後綴，例如 gpt-5.4-mini-2026-03-17 → gpt-5.4-mini。"""
    if _is_null(model_returned):
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
    context_tier: str = TIER_SHORT
    input_per_1k: float | None = None
    input_confidence: str = CONF_CONFIRMED
    cached_input_per_1k: float | None = None
    cached_input_confidence: str = CONF_CONFIRMED
    cache_write_per_1k: float | None = None
    cache_write_confidence: str = CONF_CONFIRMED
    output_per_1k: float | None = None
    output_confidence: str = CONF_CONFIRMED
    expires_at: str = ""
    note: str = ""
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
        # 兩層 key：(family, context_tier)。長短上下文是**平行的兩組價目**，
        # 不是同一組的修正——同一個 family 可以只有短沒有長（官方未列級距時
        # 一律用短），所以不能把長上下文當成短的一個係數。
        self._by_key: dict[tuple[str, str], list[PriceRow]] = {}
        for row in rows:
            self._by_key.setdefault((row.model_family, row.context_tier),
                                    []).append(row)
        for group in self._by_key.values():
            # None（未指定生效日）排最前面，代表「一直都適用」。
            group.sort(key=lambda r: (r.effective_date is not None,
                                      r.effective_date or date.min))

    def __len__(self) -> int:
        return sum(len(g) for g in self._by_key.values())

    @property
    def families(self) -> list[str]:
        return sorted({family for family, _ in self._by_key})

    @property
    def priced_families(self) -> list[str]:
        return sorted({family for (family, _), g in self._by_key.items()
                       if any(r.has_any_price for r in g)})

    def lookup(self, family: str | None, on_date: date | None,
               tier: str = TIER_SHORT) -> PriceRow | None:
        """取某個 family 在某日、某個上下文級距適用的價目列。

        找不到長上下文價目時**退回短上下文**：官方只為部分模型列出長上下文
        級距，沒列的就是沿用同一組價格，那是規則不是缺漏。
        退回時不降 price_confidence——價格本身仍是官方明列的。

        on_date 為 None（請求沒有可解析的時間）時只有在該組只有一列價目時
        才回答；有多列就是不知道該用哪一段，回 None 讓上層標成未定價。
        """
        if not family:
            return None
        row = self._lookup_tier(family, on_date, tier)
        if row is None and tier != TIER_SHORT:
            row = self._lookup_tier(family, on_date, TIER_SHORT)
        return row

    def _lookup_tier(self, family: str, on_date: date | None,
                     tier: str) -> PriceRow | None:
        group = self._by_key.get((family, tier))
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
        # 檔頭可以有 # 註解行，說明空白欄的三種意義（見該檔開頭）。
        # **只吃掉標頭之前的註解**，不是全檔過濾：note 欄若有跨行的引號內容，
        # 其中一行剛好以 # 開頭就會被誤刪，而那種錯不會報，只會少一列價目。
        lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        start = 0
        while start < len(lines) and lines[start].lstrip().startswith("#"):
            start += 1
        with io.StringIO("".join(lines[start:]), newline="") as handle:
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
                prices = {
                    field: _as_price(raw.get(field))
                    for field in ("input_price_per_1k",
                                  "cached_input_price_per_1k",
                                  "cache_write_price_per_1k",
                                  "output_price_per_1k")
                }

                def conf(field: str, column: str) -> str:
                    return _conf(raw.get(column), price=prices[field],
                                 field=field, family=family, line_no=line_no)

                rows.append(PriceRow(
                    model_family=family,
                    effective_date=_as_date(raw.get("effective_date")),
                    context_tier=((raw.get("context_tier") or "").strip()
                                  or TIER_SHORT),
                    input_per_1k=prices["input_price_per_1k"],
                    input_confidence=conf("input_price_per_1k",
                                          "input_confidence"),
                    cached_input_per_1k=prices["cached_input_price_per_1k"],
                    cached_input_confidence=conf("cached_input_price_per_1k",
                                                 "cached_input_confidence"),
                    cache_write_per_1k=prices["cache_write_price_per_1k"],
                    cache_write_confidence=conf("cache_write_price_per_1k",
                                                "cache_write_confidence"),
                    output_per_1k=prices["output_price_per_1k"],
                    output_confidence=conf("output_price_per_1k",
                                           "output_confidence"),
                    expires_at=(raw.get("expires_at") or "").strip(),
                    note=(raw.get("note") or "").strip(),
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
    cache_write_tokens: int = 0
    context_tier: str = TIER_SHORT
    input_cost: float | None = None
    cached_cost: float | None = None
    cache_write_cost: float | None = None
    output_cost: float | None = None
    cost: float | None = None
    price_effective_date: date | None = None
    # 四段各自的價目可靠度。與 pricing_status 分開：後者說「能不能算」，
    # 這裡說「算出來的那個數字有多硬」；而且是逐段的，因為同一列價目的
    # 四個價位來源可以不同。
    input_confidence: str = CONF_UNPRICED
    cached_confidence: str = CONF_UNPRICED
    cache_write_confidence: str = CONF_UNPRICED
    output_confidence: str = CONF_UNPRICED
    price_expires_at: str = ""
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
    cache_write_raw = _field(row, "cache_write_tokens")
    cache_write_tokens = _as_int(cache_write_raw)

    # 級距依 prompt_tokens 判定（含快取命中的部分）：長上下文加價是為了
    # 承載那個 context window，不管其中多少來自快取。
    tier = TIER_LONG if prompt_tokens > LONG_CONTEXT_THRESHOLD else TIER_SHORT

    note = ""
    if _is_null(cache_write_raw):
        # 5.6 家族的 cache_write 覆蓋率 94~98%，缺的那些視為 0 並標記——
        # 不標的話「這筆沒有快取寫入」與「這筆沒回報快取寫入」看起來一樣。
        note = "cache_write_tokens 未回報，以 0 計"
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
            cache_write_tokens=cache_write_tokens,
            context_tier=tier,
            note="；".join(p for p in (note, extra) if p),
        )

    # 規則順序即判斷順序：本地模型不論打哪個端點都不計價。
    if str(provider or "").lower() in LOCAL_PROVIDERS:
        return unpriced(STATUS_UNPRICED_LOCAL)

    if endpoint in NON_TOKEN_ENDPOINTS:
        return unpriced(STATUS_UNPRICED_NON_TOKEN)

    price = pricing_table.lookup(family, on_date, tier)
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

    # 快取寫入只有 5.6 家族收費，其餘模型該欄留空即視為不計。
    # 留空與 0 在這裡語意相同（都不產生金額），所以不擋——差別在於
    # 「有 token 但沒填價」對這一段是正常情形，不是缺漏。
    billable_cache_write = (cache_write_tokens
                            if price.cache_write_per_1k is not None else 0)

    input_cost = uncached / 1000 * price.input_per_1k
    cached_cost = cached_tokens / 1000 * (price.cached_input_per_1k or 0.0)
    cache_write_cost = billable_cache_write / 1000 * (price.cache_write_per_1k or 0.0)
    output_cost = completion_tokens / 1000 * (price.output_per_1k or 0.0)

    return CostEstimate(
        pricing_status=STATUS_PRICED,
        confidence=confidence,
        model_family=family,
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        uncached_prompt_tokens=uncached,
        completion_tokens=completion_tokens,
        cache_write_tokens=cache_write_tokens,
        context_tier=price.context_tier,
        input_cost=input_cost,
        cached_cost=cached_cost,
        cache_write_cost=cache_write_cost,
        output_cost=output_cost,
        cost=input_cost + cached_cost + cache_write_cost + output_cost,
        price_effective_date=price.effective_date,
        input_confidence=price.input_confidence,
        cached_confidence=price.cached_input_confidence,
        cache_write_confidence=price.cache_write_confidence,
        output_confidence=price.output_confidence,
        price_expires_at=price.expires_at,
        note=note,
    )


# 四段的 (金額欄, confidence 欄) 對照。彙總與逐筆輸出都靠它，不要各寫一份。
COST_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("input_cost", "input_confidence", "未快取 prompt"),
    ("cached_cost", "cached_confidence", "快取讀"),
    ("cache_write_cost", "cache_write_confidence", "快取寫"),
    ("output_cost", "output_confidence", "輸出"),
)


def confidence_breakdown(result):
    """按**段金額**加權彙總 price_confidence。回傳 DataFrame。

    為什麼不能用「筆數 × 該筆的 confidence」
    ----------------------------------------
    一筆請求的金額由四段組成，四段的價目來源可以不同。8/22 前的
    gpt-5.6-sol 只有快取寫入是推估價，其餘三段都是官方明列——整筆算成
    inferred 會得出「51.8% 的金額建立在推估價上」（$1,424.65），而按段
    加權的實際值是 13.55%（$372.40）。前者會讓讀者質疑整份估算，而那個
    質疑是我們自己製造的。

    這兩個數字本身也是一課：初次寫下時記的是 50.2% / $1,369.89，那是
    **價目表補齊 12 個 model_family 之前**算的——當時有一批請求還落在
    unpriced_no_table，沒有金額可以歸屬到任何一桶，總額也還不是
    $2,749.32。補齊之後整列歸屬的金額升到 $1,424.65。**兩個版本的對比
    才是本函式存在的理由，所以舊值留著當對照，不是刪掉換新的。**
    要重現舊值需要同時回退價目表，光看程式看不出來。

    代價是**同一筆請求會同時出現在多個桶裡**（它的錢確實一部分來自明列價、
    一部分來自推估價），所以「涉及筆數」那一欄跨列相加會超過總筆數。
    欄名寫明了這件事；金額欄則是可加總的，四個桶相加等於總額。
    """
    import pandas as pd

    rows: dict[str, dict] = {}
    for cost_col, conf_col, _label in COST_SEGMENTS:
        amount = result[cost_col].fillna(0)
        for conf, part in amount.groupby(result[conf_col]):
            value = float(part.sum())
            if value == 0 and conf == CONF_UNPRICED:
                continue
            bucket = rows.setdefault(conf, {"金額": 0.0, "涉及筆數": 0})
            bucket["金額"] += value
            bucket["涉及筆數"] += int((part > 0).sum())

    table = pd.DataFrame(rows).T.rename_axis("price_confidence").reset_index()
    if table.empty:
        return table
    total = table["金額"].sum()
    table["金額%"] = table["金額"] / total if total else 0.0
    return table.sort_values("金額", ascending=False).reset_index(drop=True)


def estimate_frame(frame, pricing_table: PricingTable):
    """對整個 DataFrame 逐列估算，回傳同長度的結果 DataFrame。

    刻意逐列呼叫 estimate_cost 而不另外寫一份向量化版本：向量化會快個幾十倍，
    但驗證報告就不再是在驗證真正上線的那個函式了。12 萬列跑幾秒，還不值得換。
    """
    import pandas as pd

    records = [vars(estimate_cost(row, pricing_table))
               for row in frame.to_dict("records")]
    return pd.DataFrame.from_records(records, index=frame.index)
