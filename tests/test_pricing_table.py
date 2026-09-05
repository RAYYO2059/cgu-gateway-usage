"""價目表載入的測試，重點在 confidence 的失效方向。

confidence 這一欄存在的唯一理由，是讓「這個價格哪來的」不必靠人記得。
所以它的預設值必須往安全的方向失效：有價格卻沒標來源要當場擋下來，
而不是靜默變成「官方明列」——後者會讓一個推估價在報告裡冒充官方價。

合成 csv，不讀 ref/ 底下任何檔案，clone 下來即可執行。
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import pricing  # noqa: E402

COLS = list(pricing.PRICING_COLUMNS)


def write_table(rows: list[dict]) -> Path:
    target = Path(tempfile.mkdtemp()) / "pricing.csv"
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLS)
        for row in rows:
            full = dict.fromkeys(COLS, "")
            full.update(row)
            writer.writerow([full[c] for c in COLS])
    return target


def load(rows: list[dict]) -> pricing.PricingTable:
    return pricing.PricingTable.from_csv(write_table(rows))


# --- confidence 的失效方向 ---------------------------------------------------
def test_有價格但confidence空白要raise():
    try:
        load([{"model_family": "x", "input_price_per_1k": "0.001"}])
    except ValueError as exc:
        assert "必須明確標示來源" in str(exc)
        assert "不會預設成 confirmed" in str(exc)
    else:
        raise AssertionError("有價格卻沒標 confidence 應該 raise")


def test_每個價位各自檢查():
    # 只漏 output 的 confidence，其餘都有。
    try:
        load([{"model_family": "x",
               "input_price_per_1k": "0.001", "input_confidence": "confirmed",
               "output_price_per_1k": "0.004"}])
    except ValueError as exc:
        assert "output_price_per_1k" in str(exc)
    else:
        raise AssertionError("漏標 output 的 confidence 應該 raise")


def test_無價格無confidence不raise():
    # 沒有價格的價位不會被用到，confidence 是什麼都無所謂。
    table = load([{"model_family": "y"}])
    assert len(table) == 1


def test_無價格的價位confidence記為unpriced():
    table = load([{"model_family": "y",
                   "input_price_per_1k": "0.001",
                   "input_confidence": "confirmed"}])
    row = table.lookup("y", None)
    assert row.input_confidence == "confirmed"
    # 沒填價的三段不該冒充 confirmed。
    assert row.cache_write_confidence == pricing.CONF_UNPRICED
    assert row.output_confidence == pricing.CONF_UNPRICED


def test_非法的confidence值要raise():
    try:
        load([{"model_family": "z", "input_price_per_1k": "0.001",
               "input_confidence": "probably"}])
    except ValueError as exc:
        assert "不是合法值" in str(exc)
    else:
        raise AssertionError("非法 confidence 值應該 raise")


def test_四個合法值都收():
    for value in sorted(pricing.VALID_CONFIDENCE):
        table = load([{"model_family": "m", "input_price_per_1k": "0.001",
                       "input_confidence": value}])
        assert table.lookup("m", None).input_confidence == value


# --- 級距與生效日 ------------------------------------------------------------
def test_長上下文無價時退回短上下文():
    table = load([{"model_family": "a", "context_tier": "short",
                   "input_price_per_1k": "0.002",
                   "input_confidence": "confirmed"}])
    row = table.lookup("a", None, pricing.TIER_LONG)
    assert row is not None
    assert row.context_tier == pricing.TIER_SHORT
    assert row.input_per_1k == 0.002


def test_長上下文有價時用長上下文():
    table = load([
        {"model_family": "a", "context_tier": "short",
         "input_price_per_1k": "0.002", "input_confidence": "confirmed"},
        {"model_family": "a", "context_tier": "long",
         "input_price_per_1k": "0.004", "input_confidence": "confirmed"},
    ])
    assert table.lookup("a", None, pricing.TIER_LONG).input_per_1k == 0.004
    assert table.lookup("a", None, pricing.TIER_SHORT).input_per_1k == 0.002


def test_生效日分段():
    import datetime

    table = load([
        {"model_family": "a", "input_price_per_1k": "0.005",
         "input_confidence": "confirmed"},
        {"model_family": "a", "effective_date": "2026-08-22",
         "input_price_per_1k": "0.004", "input_confidence": "promotional"},
    ])
    before = table.lookup("a", datetime.date(2026, 8, 21))
    on_day = table.lookup("a", datetime.date(2026, 8, 22))
    assert before.input_per_1k == 0.005
    assert before.input_confidence == "confirmed"
    assert on_day.input_per_1k == 0.004
    assert on_day.input_confidence == "promotional"


# --- 四段計價 ----------------------------------------------------------------
def _row(**kw):
    base = {"provider": "openai", "endpoint": "/v1/responses",
            "model_returned": "a", "prompt_tokens": 1000, "cached_tokens": 400,
            "completion_tokens": 500, "cache_write_tokens": 200,
            "date_taipei": "2026-08-10"}
    base.update(kw)
    return base


FOUR_SEGMENT = [{"model_family": "a",
                 "input_price_per_1k": "1", "input_confidence": "confirmed",
                 "cached_input_price_per_1k": "0.1",
                 "cached_input_confidence": "confirmed",
                 "cache_write_price_per_1k": "1.25",
                 "cache_write_confidence": "inferred",
                 "output_price_per_1k": "2", "output_confidence": "confirmed"}]


def test_四段金額與總額():
    result = pricing.estimate_cost(_row(), load(FOUR_SEGMENT))
    assert result.pricing_status == pricing.STATUS_PRICED
    assert result.input_cost == 600 / 1000 * 1
    assert result.cached_cost == 400 / 1000 * 0.1
    assert result.cache_write_cost == 200 / 1000 * 1.25
    assert result.output_cost == 500 / 1000 * 2
    assert round(result.cost, 10) == round(0.6 + 0.04 + 0.25 + 1.0, 10)


def test_每段各自帶confidence():
    result = pricing.estimate_cost(_row(), load(FOUR_SEGMENT))
    assert result.input_confidence == "confirmed"
    assert result.cache_write_confidence == "inferred"
    # 整筆不該被單一價位的 inferred 汙染——這正是按段標記的理由。
    assert result.output_confidence == "confirmed"


def test_沒有快取寫價時該段不計費():
    table = load([{"model_family": "a",
                   "input_price_per_1k": "1", "input_confidence": "confirmed",
                   "cached_input_price_per_1k": "0.1",
                   "cached_input_confidence": "confirmed",
                   "output_price_per_1k": "2", "output_confidence": "confirmed"}])
    result = pricing.estimate_cost(_row(), table)
    # 有 cache_write_tokens 但該模型不收此費用 → 不擋、不計價。
    assert result.cache_write_tokens == 200
    assert result.cache_write_cost == 0
    assert result.pricing_status == pricing.STATUS_PRICED


def test_長上下文走另一組價():
    table = load([
        {"model_family": "a", "context_tier": "short",
         "input_price_per_1k": "1", "input_confidence": "confirmed",
         "output_price_per_1k": "2", "output_confidence": "confirmed"},
        {"model_family": "a", "context_tier": "long",
         "input_price_per_1k": "2", "input_confidence": "confirmed",
         "output_price_per_1k": "4", "output_confidence": "confirmed"},
    ])
    short = pricing.estimate_cost(
        _row(prompt_tokens=1000, cached_tokens=0, cache_write_tokens=0), table)
    long = pricing.estimate_cost(
        _row(prompt_tokens=pricing.LONG_CONTEXT_THRESHOLD + 1,
             cached_tokens=0, cache_write_tokens=0), table)
    assert short.context_tier == pricing.TIER_SHORT
    assert long.context_tier == pricing.TIER_LONG
    assert long.input_cost / (pricing.LONG_CONTEXT_THRESHOLD + 1) == 2 / 1000


def test_門檻是嚴格大於():
    # 必須用「有 long 列」的價目表：沒有 long 列時 lookup 會退回 short，
    # 於是 context_tier 永遠回報 short，門檻改成 >= 也測不出來。
    table = load([
        {"model_family": "a", "context_tier": "short",
         "input_price_per_1k": "1", "input_confidence": "confirmed",
         "output_price_per_1k": "2", "output_confidence": "confirmed"},
        {"model_family": "a", "context_tier": "long",
         "input_price_per_1k": "2", "input_confidence": "confirmed",
         "output_price_per_1k": "4", "output_confidence": "confirmed"},
    ])
    at = pricing.estimate_cost(
        _row(prompt_tokens=pricing.LONG_CONTEXT_THRESHOLD,
             cached_tokens=0, cache_write_tokens=0), table)
    over = pricing.estimate_cost(
        _row(prompt_tokens=pricing.LONG_CONTEXT_THRESHOLD + 1,
             cached_tokens=0, cache_write_tokens=0), table)
    # 現有行為是 prompt_tokens > 門檻，所以「正好等於」仍算短上下文。
    assert at.context_tier == pricing.TIER_SHORT
    assert at.input_cost == pricing.LONG_CONTEXT_THRESHOLD / 1000 * 1
    assert over.context_tier == pricing.TIER_LONG
    assert over.input_cost == (pricing.LONG_CONTEXT_THRESHOLD + 1) / 1000 * 2


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通過")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())


# ---------------------------------------------------------------------------
# 出貨的那張表本身
# ---------------------------------------------------------------------------
def test_實際出貨的價目表載得起來():
    """上面所有測試都是拿合成的表在跑，沒有一個碰過 ref/pricing_table.csv。

    這個缺口真的咬過一次：2026-08-27 在表頭加了 # 註解說明空白欄的意義，
    14 個測試全部通過，而正式載入器 from_csv() 直接 raise——因為 csv.DictReader
    把註解行當成了標頭。**測試綠燈與程式能不能讀那張表是兩件事。**
    """
    table = pricing.PricingTable.from_csv()
    assert table is not None


def test_出貨的價目表能算出金額():
    """能載入不等於能用：欄位齊全但價格全空的表也載得起來。"""
    table = pricing.PricingTable.from_csv()
    row = {
        "provider": "openai",
        "endpoint": "/v1/chat/completions",
        "model_returned": "gpt-4o",
        "prompt_tokens": 1000,
        "cached_tokens": 0,
        "completion_tokens": 1000,
        "date_taipei": "2026-08-20",
    }
    estimate = pricing.estimate_cost(row, table)
    assert estimate.pricing_status == pricing.STATUS_PRICED
    # gpt-4o：輸入 $2.50/1M、輸出 $10.00/1M → 1k + 1k = 0.0025 + 0.01
    assert round(estimate.cost, 6) == 0.0125


def test_檔頭註解不會吃掉資料列():
    """只吃標頭之前的註解行，不是全檔過濾。"""
    table = pricing.PricingTable.from_csv()
    families = set()
    for value in vars(table).values():
        if isinstance(value, dict):
            families.update(str(k) for k in value)
        elif isinstance(value, list):
            families.update(str(getattr(r, "model_family", "")) for r in value)
    # 表頭註解裡出現過這些名字，若被當成資料列會混進來
    assert not any(f.startswith("#") for f in families), "註解行被當成資料列"
