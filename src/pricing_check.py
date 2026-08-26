"""在全量 lite 資料上跑一次 estimate_cost，只驗證管線邏輯，不看金額。

價目表全空的時候跑，預期結果是「所有 token 計價的請求都落在
unpriced_no_table」；價目填進去之後再跑一次，那一格應該搬到 priced。
兩次都要看不變量那一段全過。

    python -m src.pricing_check data/01_request_lite
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src import pricing

SAMPLE_SIZE = 20


# lite_index 快照用點號欄位名（usage.prompt_tokens），而 data/01_request_lite
# 的 L1 輸出是攤平的（prompt_tokens）。這裡只做欄位名解析，取值邏輯仍在
# pricing._FIELD_ALIASES，兩邊共用同一份對照，不各自維護。
def col(frame: pd.DataFrame, name: str) -> pd.Series:
    for key in pricing._FIELD_ALIASES[name]:
        if key in frame.columns:
            return frame[key]
    raise KeyError(
        f"資料裡找不到 {name} 對應的欄位，試過 {pricing._FIELD_ALIASES[name]}")


def _k(row, name: str) -> str:
    """單列（Series）版的欄位名解析。"""
    for key in pricing._FIELD_ALIASES[name]:
        if key in row.index:
            return key
    raise KeyError(name)


def _fmt_int(value: float) -> str:
    return f"{int(value):,}"


# 四個金額欄。從 pricing.COST_SEGMENTS 推導，不另外抄一份——抄了之後
# 新增一段時這裡會安靜地少驗一欄。
_COST_COLUMNS = [c for c, _, _ in pricing.COST_SEGMENTS] + ["cost"]

# 只有這三個 family 收快取寫入費（官方對 5.6 家族列出該價位）。
_CACHE_WRITE_FAMILIES = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")

# 基準值。它們不是「應該長這樣」而是「2026-08-26 這批資料實測就是這樣」，
# 對不上代表資料換了或分段邏輯壞了，兩種都要有人看一眼。
_LONG_CONTEXT_ROWS = 102
_LONG_CONTEXT_BILLABLE = 4_489_614
_SOL_BEFORE_ROWS = 15_620
_SOL_AFTER_ROWS = 2_209
_SOL_SPLIT_DATE = "2026-08-22"

# 四個 confidence 桶的金額基準（美元）。
#
# 光有「inferred 只來自快取寫」擋不住把 confidence 掛回整列的改動：那樣改之後
# 8/22 前 sol 的四段會一起變成 confirmed，於是 inferred 整個消失，而
# 「只來自快取寫」在沒有任何 inferred 時是**空真**，安靜地通過。
# 所以要同時釘住金額——空集合過不了「等於 372.40」。
_CONFIDENCE_BASELINE = {
    "confirmed": 2136.81,
    "inferred": 372.40,
    "promotional": 193.85,
    "provisional": 26.03,
}
_BASELINE_TOLERANCE = 0.01


def _billable(result: pd.DataFrame) -> pd.Series:
    return (result["uncached_prompt_tokens"] + result["completion_tokens"])


def _long_context_matches(frame: pd.DataFrame, result: pd.DataFrame) -> bool:
    long_rows = result["context_tier"] == pricing.TIER_LONG
    if int(long_rows.sum()) != _LONG_CONTEXT_ROWS:
        return False
    # 用原始 prompt_tokens 判定，不用 context_tier——後者回報的是「實際採用
    # 的價目列級距」，而該模型沒有長上下文價時會退回 short，兩者會分歧。
    over = col(frame, "prompt_tokens").fillna(0) > pricing.LONG_CONTEXT_THRESHOLD
    return int(_billable(result)[over].sum()) == _LONG_CONTEXT_BILLABLE


def _sol_split_matches(frame: pd.DataFrame, result: pd.DataFrame) -> bool:
    sol = result["model_family"] == "gpt-5.6-sol"
    if "date_taipei" not in frame.columns:
        return True  # 沒有分區鍵的資料集不驗這條
    day = frame["date_taipei"].astype("string")
    before = int((sol & (day < _SOL_SPLIT_DATE)).sum())
    after = int((sol & (day >= _SOL_SPLIT_DATE)).sum())
    return before == _SOL_BEFORE_ROWS and after == _SOL_AFTER_ROWS


def _confidence_sums_to_total(result: pd.DataFrame) -> bool:
    breakdown = pricing.confidence_breakdown(result)
    if breakdown.empty:
        return True
    return abs(float(breakdown["金額"].sum())
               - float(result["cost"].fillna(0).sum())) < 1e-6


def _confidence_matches_baseline(result: pd.DataFrame) -> bool:
    """四個桶的金額都要對得上基準，桶本身也不能消失。

    這條與「inferred 只來自快取寫」互補：那條防「inferred 擴散到其他段」，
    這條防「inferred 整個不見」。只有前者的話，把 confidence 掛回整列會讓
    inferred 消失而檢查空真通過——金額完全不變，沒有任何其他徵兆。
    """
    breakdown = pricing.confidence_breakdown(result)
    if breakdown.empty:
        return False
    actual = dict(zip(breakdown["price_confidence"], breakdown["金額"]))
    if set(actual) != set(_CONFIDENCE_BASELINE):
        return False
    return all(abs(float(actual[k]) - v) < _BASELINE_TOLERANCE
               for k, v in _CONFIDENCE_BASELINE.items())


def _inferred_only_cache_write(result: pd.DataFrame) -> bool:
    """inferred 只該出現在快取寫那一段。

    這條在守一個具體的錯誤：把 confidence 掛回「整列」而不是「每個價位」。
    真那樣改的話，8/22 前 gpt-5.6-sol 的四段都會變成 inferred，
    彙總會從 13.65% 跳回 50.2%，而金額本身完全不變、不會有其他徵兆。
    """
    for cost_col, conf_col, _label in pricing.COST_SEGMENTS:
        if cost_col == "cache_write_cost":
            continue
        tainted = result.loc[result[conf_col] == pricing.CONF_INFERRED, cost_col]
        if float(tainted.fillna(0).sum()) != 0:
            return False
    return True


def build_report(frame: pd.DataFrame, result: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    total_rows = len(frame)
    tokens = (col(frame, "prompt_tokens").fillna(0)
              + col(frame, "completion_tokens").fillna(0))
    total_tokens = tokens.sum()

    lines.append(f"資料：{_fmt_int(total_rows)} 列，"
                 f"prompt+completion 合計 {_fmt_int(total_tokens)} token")

    # --- 1. pricing_status 分布 ---
    lines.append("")
    lines.append("pricing_status 分布")
    lines.append(f"  {'status':<22}{'筆數':>10}{'筆數佔比':>10}"
                 f"{'token':>18}{'token佔比':>11}")
    grouped = pd.DataFrame({"status": result["pricing_status"], "tok": tokens})
    summary = grouped.groupby("status").agg(n=("tok", "size"), tok=("tok", "sum"))
    for status in (pricing.STATUS_PRICED, pricing.STATUS_UNPRICED_LOCAL,
                   pricing.STATUS_UNPRICED_NON_TOKEN,
                   pricing.STATUS_UNPRICED_NO_TABLE):
        n = int(summary["n"].get(status, 0))
        tok = float(summary["tok"].get(status, 0.0))
        lines.append(f"  {status:<22}{_fmt_int(n):>10}{n / total_rows:>9.2%}"
                     f"{_fmt_int(tok):>18}{tok / total_tokens:>10.2%}")
    unexpected = set(summary.index) - {
        pricing.STATUS_PRICED, pricing.STATUS_UNPRICED_LOCAL,
        pricing.STATUS_UNPRICED_NON_TOKEN, pricing.STATUS_UNPRICED_NO_TABLE}
    if unexpected:
        lines.append(f"  !! 出現預期外的 status：{sorted(unexpected)}")

    # --- 2. confidence 分布 ---
    lines.append("")
    lines.append("confidence 分布")
    conf = pd.DataFrame({"c": result["confidence"], "tok": tokens}).groupby("c").agg(
        n=("tok", "size"), tok=("tok", "sum"))
    for value, part in conf.iterrows():
        lines.append(f"  {value:<22}{_fmt_int(part['n']):>10}"
                     f"{part['n'] / total_rows:>9.2%}"
                     f"{_fmt_int(part['tok']):>18}{part['tok'] / total_tokens:>10.2%}")

    low = result["confidence"] == pricing.CONFIDENCE_LOW
    ok2xx = col(frame, "status_code").between(200, 299)
    zero = tokens == 0
    non_token = col(frame, "endpoint").isin(pricing.NON_TOKEN_ENDPOINTS)
    lines.append(f"  低信心那兩天的 2xx 請求 {_fmt_int((low & ok2xx).sum())} 筆，"
                 f"其中 token 為 0 的 {_fmt_int((low & ok2xx & zero).sum())} 筆"
                 f"（扣掉非 token 端點後 "
                 f"{_fmt_int((low & ok2xx & zero & ~non_token).sum())} 筆）")

    # --- 3. 不變量 ---
    lines.append("")
    lines.append("不變量")
    priced = result["pricing_status"] == pricing.STATUS_PRICED
    checks = [
        ("uncached + cached == prompt_tokens",
         (result["uncached_prompt_tokens"] + result["cached_tokens"]
          == result["prompt_tokens"]).all()),
        ("uncached 不為負",
         (result["uncached_prompt_tokens"] >= 0).all()),
        ("cached 不超過 prompt",
         (result["cached_tokens"] <= result["prompt_tokens"]).all()),
        ("token 中間值與原始欄位一致",
         (result["prompt_tokens"].eq(col(frame, "prompt_tokens").fillna(0)).all()
          and result["completion_tokens"].eq(
              col(frame, "completion_tokens").fillna(0)).all())),
        ("cached_tokens 為 null 的列一律當 0",
         (result.loc[col(frame, "cached_tokens").isna(), "cached_tokens"] == 0).all()),
        ("ollama 全部標 unpriced_local",
         (result.loc[col(frame, "provider") == "ollama", "pricing_status"]
          == pricing.STATUS_UNPRICED_LOCAL).all()),
        ("非 token 端點全部標 unpriced_non_token（本地模型除外）",
         (result.loc[non_token & (col(frame, "provider") != "ollama"),
                     "pricing_status"] == pricing.STATUS_UNPRICED_NON_TOKEN).all()),
        ("只有 priced 有金額",
         result.loc[~priced, _COST_COLUMNS].isna().all().all()),
        ("priced 的金額都不為 null",
         result.loc[priced, _COST_COLUMNS].notna().all().all()
         if priced.any() else True),
        ("priced 的四段相加等於總額",
         bool((result.loc[priced, "cost"] - sum(
             result.loc[priced, c] for c, _, _ in pricing.COST_SEGMENTS)
         ).abs().lt(1e-9).all()) if priced.any() else True),

        # --- 四段化之後才成立的 ---
        ("快取寫金額只可能來自 gpt-5.6 家族",
         bool((result.loc[~result["model_family"].isin(_CACHE_WRITE_FAMILIES),
                          "cache_write_cost"].fillna(0) == 0).all())),
        ("長上下文筆數與計費 token 符合基準",
         _long_context_matches(frame, result)),
        ("gpt-5.6-sol 依 date_taipei 切在 2026-08-22",
         _sol_split_matches(frame, result)),
        ("confidence 四桶金額相加等於總額",
         _confidence_sums_to_total(result)),
        ("inferred 的金額全部來自快取寫這一段",
         _inferred_only_cache_write(result)),
        ("confidence 四桶金額符合基準",
         _confidence_matches_baseline(result)),
    ]
    failed = 0
    for label, passed in checks:
        lines.append(f"  {'ok  ' if passed else 'FAIL'} {label}")
        failed += not passed

    # --- 4. 抽樣看中間值 ---
    lines.append("")
    lines.append(f"抽 {SAMPLE_SIZE} 筆看拆分中間值")
    sample = _pick_sample(frame, result, tokens)
    for label, idx in sample:
        f = frame.loc[idx]
        r = result.loc[idx]
        raw_cached = f[_k(f, 'cached_tokens')]
        cached_repr = "null" if pd.isna(raw_cached) else _fmt_int(raw_cached)
        lines.append(
            f"  [{label}] {f[_k(f, 'provider')]} {f[_k(f, 'endpoint')]} "
            f"{r['model_family']} @{pricing.taipei_date(f)}")
        lines.append(
            f"      prompt={_fmt_int(f[_k(f, 'prompt_tokens')])} "
            f"cached_raw={cached_repr} -> cached={_fmt_int(r['cached_tokens'])} "
            f"uncached={_fmt_int(r['uncached_prompt_tokens'])} "
            f"completion={_fmt_int(f[_k(f, 'completion_tokens')])}")
        cost = ("cost=未定價" if r["cost"] is None or pd.isna(r["cost"])
                else f"cost={r['cost']:.6f}"
                      f"（in {r['input_cost']:.6f} + cached {r['cached_cost']:.6f}"
                      f" + out {r['output_cost']:.6f}）")
        lines.append(f"      {r['pricing_status']} / {r['confidence']} / {cost}"
                     + (f" / {r['note']}" if r["note"] else ""))

    return lines if not failed else lines + [f"\n!! {failed} 項不變量沒過"]


def unpriced_report(frame: pd.DataFrame, result: pd.DataFrame) -> list[str]:
    """未定價的 family 明細。

    這一段在守一個這個專案踩過的坑：ollama 那裡曾經把「沒有價格」寫成 0，
    而 0 在報表上讀起來是「免費」。所以未定價的列必須是 NA 而不是 0，
    而且要能一眼看出它們涉及多少量——量夠小才可以說「不影響結論」。
    """
    lines = ["", "未定價的 model_family（確認落在 unpriced_no_table 而非以 0 計價）"]
    no_table = result["pricing_status"] == pricing.STATUS_UNPRICED_NO_TABLE
    billable_all = _billable(result)
    total_billable = float(billable_all.sum())

    if not no_table.any():
        lines.append("  無")
        return lines

    grouped = pd.DataFrame({
        "family": result.loc[no_table, "model_family"],
        "billable": billable_all[no_table],
    }).groupby("family", dropna=False).agg(
        筆數=("billable", "size"), 計費token=("billable", "sum"),
    ).sort_values("計費token", ascending=False)

    lines.append(f"  {'model_family':<26}{'筆數':>8}{'計費token':>14}{'佔全體':>9}")
    for family, row in grouped.iterrows():
        label = "(model_returned 為空)" if pd.isna(family) else str(family)
        lines.append(f"  {label:<26}{int(row['筆數']):>8,}"
                     f"{int(row['計費token']):>14,}"
                     f"{row['計費token'] / total_billable:>9.3%}")
    total_rows = int(no_table.sum())
    total_tok = float(billable_all[no_table].sum())
    lines.append(f"  {'合計':<26}{total_rows:>8,}{int(total_tok):>14,}"
                 f"{total_tok / total_billable:>9.2%}")

    # 這幾條是重點：未定價 = 沒有金額，不是金額為 0。
    checks = [
        ("未定價的列金額為 NA 而不是 0",
         bool(result.loc[no_table, _COST_COLUMNS].isna().all().all())),
        ("未定價的列不計入總額",
         abs(float(result.loc[no_table, "cost"].fillna(0).sum())) < 1e-12),
        ("未定價的列 price_confidence 為 unpriced",
         bool((result.loc[no_table, [c for _, c, _ in pricing.COST_SEGMENTS]]
               == pricing.CONF_UNPRICED).all().all())),
    ]
    for label, passed in checks:
        lines.append(f"  {'ok  ' if passed else 'FAIL'} {label}")
    return lines


def _pick_sample(frame, result, tokens) -> list[tuple[str, object]]:
    """分層抽樣，確保每種分支都被看到；同層內取 token 最多的，結果可重現。"""
    prov = col(frame, "provider")
    cached_raw = col(frame, "cached_tokens")
    strata = [
        ("快取命中", (prov == "openai") & (cached_raw.fillna(0) > 0), 4),
        ("cached=0", (prov == "openai") & cached_raw.notna() & (cached_raw == 0), 3),
        ("cached=null", (prov == "openai") & cached_raw.isna(), 3),
        ("本地", prov == "ollama", 3),
        ("非token端點", col(frame, "endpoint").isin(pricing.NON_TOKEN_ENDPOINTS), 3),
        ("低信心", (result["confidence"] == pricing.CONFIDENCE_LOW) & (tokens == 0), 2),
        ("embeddings", col(frame, "endpoint") == "/v1/embeddings", 1),
    ]
    picked: list[tuple[str, object]] = []
    seen: set[object] = set()
    for label, mask, want in strata:
        candidates = tokens[mask].sort_values(ascending=False, kind="mergesort")
        for idx in candidates.index:
            if len(picked) >= SAMPLE_SIZE:
                break
            if idx in seen:
                continue
            picked.append((label, idx))
            seen.add(idx)
            want -= 1
            if want == 0:
                break
    # model_returned 為空的那筆一定要看到，它是 unpriced_no_table 的邊界。
    empty = frame.index[col(frame, "model_returned").isna()]
    if len(empty) and empty[0] not in seen:
        picked.append(("model為空", empty[0]))
    return picked[:SAMPLE_SIZE]


# 假價目：每 1k token 輸入 1 元、快取 0.1 元、輸出 2 元。
# 三個數字刻意取不同量級，任何一段被算到別段去，總額都會差一位數。
# 假價目：四個價位取不同量級，任何一段被算到別段去，總額都會差一位數。
# 長上下文用短上下文的 10 倍（不是 2 倍）——真實比例約 2 倍，用 2 的話
# 「級距選錯」與「某段乘錯」會產生相近的金額，分不出是哪個壞了。
_FAKE_INPUT, _FAKE_CACHED, _FAKE_CACHE_WRITE, _FAKE_OUTPUT = 1.0, 0.1, 0.5, 2.0
_FAKE_LONG_FACTOR = 10.0


def arithmetic_check(frame: pd.DataFrame) -> list[str]:
    """用假價目把 priced 分支跑起來，驗證拆分算式的算術。

    價目表是空的時候一筆都不會走進 priced，等於整條計價路徑沒被測到；
    真正填了價格才發現算錯，那時候看到的是一個「看起來合理」的金額。
    """
    lines = ["", "拆分算式（獨立於真實價目，用假價目跑：輸入 1.0／快取讀 0.1／"
             "快取寫 0.5／輸出 2.0 每 1k；長上下文為 10 倍）"]

    families = sorted({pricing.model_family(m)
                       for m in col(frame, "model_returned").dropna().unique()})
    rows = []
    for family in families:
        for tier, factor in ((pricing.TIER_SHORT, 1.0),
                             (pricing.TIER_LONG, _FAKE_LONG_FACTOR)):
            rows.append(pricing.PriceRow(
                model_family=family, effective_date=None, context_tier=tier,
                input_per_1k=_FAKE_INPUT * factor,
                cached_input_per_1k=_FAKE_CACHED * factor,
                cache_write_per_1k=_FAKE_CACHE_WRITE * factor,
                output_per_1k=_FAKE_OUTPUT * factor))
    fake = pricing.PricingTable(rows)
    result = pricing.estimate_frame(frame, fake)
    priced = result["pricing_status"] == pricing.STATUS_PRICED
    lines.append(f"  priced {_fmt_int(priced.sum())} 筆 "
                 f"（{priced.sum() / len(frame):.2%}），其餘為本地或非 token 端點")

    is_long = result["context_tier"] == pricing.TIER_LONG
    factor = is_long.map({True: _FAKE_LONG_FACTOR, False: 1.0})
    expected = ((result["uncached_prompt_tokens"] / 1000 * _FAKE_INPUT
                 + result["cached_tokens"] / 1000 * _FAKE_CACHED
                 + result["cache_write_tokens"] / 1000 * _FAKE_CACHE_WRITE
                 + result["completion_tokens"] / 1000 * _FAKE_OUTPUT) * factor)
    diff = (result.loc[priced, "cost"] - expected[priced]).abs()

    cached_hit = priced & (col(frame, "cached_tokens").fillna(0) > 0)
    write_hit = priced & (result["cache_write_tokens"] > 0)
    long_priced = priced & is_long
    checks = [
        ("每一筆 priced 的總額都等於四段獨立重算的結果", bool(diff.lt(1e-9).all())),
        ("有快取命中的列，快取讀金額 > 0",
         bool((result.loc[cached_hit, "cached_cost"] > 0).all())),
        ("無快取命中的列，快取讀金額為 0",
         bool((result.loc[priced & ~cached_hit, "cached_cost"] == 0).all())),
        ("有快取寫入的列，快取寫金額 > 0",
         bool((result.loc[write_hit, "cache_write_cost"] > 0).all())
         if write_hit.any() else True),
        ("無快取寫入的列，快取寫金額為 0",
         bool((result.loc[priced & ~write_hit, "cache_write_cost"] == 0).all())),
        ("長上下文的列確實套用了長上下文價",
         bool((result.loc[long_priced, "cost"]
               - expected[long_priced]).abs().lt(1e-9).all())
         and int(long_priced.sum()) == _LONG_CONTEXT_ROWS),
        ("金額為 0 的 priced 列，token 也是 0",
         bool((result.loc[priced & (result["cost"] == 0),
                          ["prompt_tokens", "completion_tokens",
                           "cache_write_tokens"]].sum(axis=1) == 0).all())),
    ]
    for label, passed in checks:
        lines.append(f"  {'ok  ' if passed else 'FAIL'} {label}")

    # 手算一筆對照，數字直接寫在報告裡，不用回頭翻程式碼。
    hit = frame.index[cached_hit]
    if len(hit):
        idx = result.loc[hit, "cost"].idxmax()
        r = result.loc[idx]
        # 級距係數必須進手算：抽到的常常是金額最大的那筆，而那多半是長上下文。
        # 漏乘係數會讓這行印出「不符」，而實際是手算錯了不是函式錯了——
        # 一個誤報的對照比沒有對照更糟。
        tier_factor = (_FAKE_LONG_FACTOR
                       if r["context_tier"] == pricing.TIER_LONG else 1.0)
        manual = ((r["uncached_prompt_tokens"] / 1000 * _FAKE_INPUT
                   + r["cached_tokens"] / 1000 * _FAKE_CACHED
                   + r["cache_write_tokens"] / 1000 * _FAKE_CACHE_WRITE
                   + r["completion_tokens"] / 1000 * _FAKE_OUTPUT) * tier_factor)
        lines.append(
            f"  手算對照（{r['context_tier']} 級距，係數 ×{tier_factor:g}）："
            f"（uncached {_fmt_int(r['uncached_prompt_tokens'])}/1000*1.0"
            f" + cached {_fmt_int(r['cached_tokens'])}/1000*0.1"
            f" + cache_write {_fmt_int(r['cache_write_tokens'])}/1000*0.5"
            f" + out {_fmt_int(r['completion_tokens'])}/1000*2.0）"
            f" ×{tier_factor:g} = {manual:.6f}，函式回 {r['cost']:.6f}"
            f"（{'相符' if abs(manual - r['cost']) < 1e-9 else '★ 不符'}）")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lite_index", type=Path,
                        help="lite 資料的路徑：data/01_request_lite 這種分區目錄，"
                             "或單一 parquet 檔")
    parser.add_argument("--pricing-table", type=Path, default=None,
                        help=f"價目表路徑，預設 {pricing.PRICING_TABLE_PATH}")
    args = parser.parse_args(argv)

    # 允許目錄（分區資料集）與單檔兩種形式；pandas 兩者都讀得動。
    if not args.lite_index.exists():
        print(f"找不到 {args.lite_index}", file=sys.stderr)
        return 2

    table = pricing.PricingTable.from_csv(args.pricing_table)
    print(f"價目表 {len(table)} 列，"
          f"其中填了價格的 model_family {len(table.priced_families)} 個"
          f"（共 {len(table.families)} 個）")

    frame = pd.read_parquet(args.lite_index)
    result = pricing.estimate_frame(frame, table)

    lines = build_report(frame, result)
    lines += unpriced_report(frame, result)
    # 一律跑，不再只在價目表全空時跑：假價目給**每一個** family 都配上四段
    # 與長短兩級距，涵蓋的分支比真實價目多——真實價目只有 12 個 family 有價，
    # 其餘 47 個永遠走不到 priced，它們的算術就沒有任何地方驗得到。
    lines += arithmetic_check(frame)
    print("\n".join(lines))

    # 價目表沒填價時，覆蓋率必然是 0，不該因此讓檢查失敗。
    return 1 if any(line.startswith("!!") or " FAIL " in line for line in lines) else 0


if __name__ == "__main__":
    raise SystemExit(main())
