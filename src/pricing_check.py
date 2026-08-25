"""在全量 lite 資料上跑一次 estimate_cost，只驗證管線邏輯，不看金額。

價目表全空的時候跑，預期結果是「所有 token 計價的請求都落在
unpriced_no_table」；價目填進去之後再跑一次，那一格應該搬到 priced。
兩次都要看不變量那一段全過。

    python -m src.pricing_check <lite_index.parquet 的路徑>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src import pricing

SAMPLE_SIZE = 20


def _fmt_int(value: float) -> str:
    return f"{int(value):,}"


def build_report(frame: pd.DataFrame, result: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    total_rows = len(frame)
    tokens = (frame["usage.prompt_tokens"].fillna(0)
              + frame["usage.completion_tokens"].fillna(0))
    total_tokens = tokens.sum()

    lines.append(f"lite_index：{_fmt_int(total_rows)} 列，"
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
    ok2xx = frame["response.status_code"].between(200, 299)
    zero = tokens == 0
    non_token = frame["request.endpoint"].isin(pricing.NON_TOKEN_ENDPOINTS)
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
         (result["prompt_tokens"].eq(frame["usage.prompt_tokens"].fillna(0)).all()
          and result["completion_tokens"].eq(
              frame["usage.completion_tokens"].fillna(0)).all())),
        ("cached_tokens 為 null 的列一律當 0",
         (result.loc[frame["usage.cached_tokens"].isna(), "cached_tokens"] == 0).all()),
        ("ollama 全部標 unpriced_local",
         (result.loc[frame["request.provider"] == "ollama", "pricing_status"]
          == pricing.STATUS_UNPRICED_LOCAL).all()),
        ("非 token 端點全部標 unpriced_non_token（本地模型除外）",
         (result.loc[non_token & (frame["request.provider"] != "ollama"),
                     "pricing_status"] == pricing.STATUS_UNPRICED_NON_TOKEN).all()),
        ("只有 priced 有金額",
         result.loc[~priced, ["input_cost", "cached_cost", "output_cost", "cost"]]
         .isna().all().all()),
        ("priced 的金額都不為 null",
         result.loc[priced, ["input_cost", "cached_cost", "output_cost", "cost"]]
         .notna().all().all() if priced.any() else True),
        ("priced 的三段相加等於總額",
         bool((result.loc[priced, "cost"] - (
             result.loc[priced, "input_cost"] + result.loc[priced, "cached_cost"]
             + result.loc[priced, "output_cost"])).abs().lt(1e-9).all())
         if priced.any() else True),
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
        raw_cached = f["usage.cached_tokens"]
        cached_repr = "null" if pd.isna(raw_cached) else _fmt_int(raw_cached)
        lines.append(
            f"  [{label}] {f['request.provider']} {f['request.endpoint']} "
            f"{r['model_family']} @{str(f['created_at'])[:10]}")
        lines.append(
            f"      prompt={_fmt_int(f['usage.prompt_tokens'])} "
            f"cached_raw={cached_repr} -> cached={_fmt_int(r['cached_tokens'])} "
            f"uncached={_fmt_int(r['uncached_prompt_tokens'])} "
            f"completion={_fmt_int(f['usage.completion_tokens'])}")
        cost = ("cost=未定價" if r["cost"] is None or pd.isna(r["cost"])
                else f"cost={r['cost']:.6f}"
                      f"（in {r['input_cost']:.6f} + cached {r['cached_cost']:.6f}"
                      f" + out {r['output_cost']:.6f}）")
        lines.append(f"      {r['pricing_status']} / {r['confidence']} / {cost}"
                     + (f" / {r['note']}" if r["note"] else ""))

    return lines if not failed else lines + [f"\n!! {failed} 項不變量沒過"]


def _pick_sample(frame, result, tokens) -> list[tuple[str, object]]:
    """分層抽樣，確保每種分支都被看到；同層內取 token 最多的，結果可重現。"""
    prov = frame["request.provider"]
    cached_raw = frame["usage.cached_tokens"]
    strata = [
        ("快取命中", (prov == "openai") & (cached_raw.fillna(0) > 0), 4),
        ("cached=0", (prov == "openai") & cached_raw.notna() & (cached_raw == 0), 3),
        ("cached=null", (prov == "openai") & cached_raw.isna(), 3),
        ("本地", prov == "ollama", 3),
        ("非token端點", frame["request.endpoint"].isin(pricing.NON_TOKEN_ENDPOINTS), 3),
        ("低信心", (result["confidence"] == pricing.CONFIDENCE_LOW) & (tokens == 0), 2),
        ("embeddings", frame["request.endpoint"] == "/v1/embeddings", 1),
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
    empty = frame.index[frame["response.model_returned"].isna()]
    if len(empty) and empty[0] not in seen:
        picked.append(("model為空", empty[0]))
    return picked[:SAMPLE_SIZE]


# 假價目：每 1k token 輸入 1 元、快取 0.1 元、輸出 2 元。
# 三個數字刻意取不同量級，任何一段被算到別段去，總額都會差一位數。
_FAKE_INPUT, _FAKE_CACHED, _FAKE_OUTPUT = 1.0, 0.1, 2.0


def arithmetic_check(frame: pd.DataFrame) -> list[str]:
    """用假價目把 priced 分支跑起來，驗證拆分算式的算術。

    價目表是空的時候一筆都不會走進 priced，等於整條計價路徑沒被測到；
    真正填了價格才發現算錯，那時候看到的是一個「看起來合理」的金額。
    """
    lines = ["", "拆分算式（價目表是空的，改用假價目跑：輸入 1.0／快取 0.1／輸出 2.0 每 1k）"]

    families = sorted({pricing.model_family(m)
                       for m in frame["response.model_returned"].dropna().unique()})
    fake = pricing.PricingTable([
        pricing.PriceRow(model_family=f, effective_date=None,
                         input_per_1k=_FAKE_INPUT,
                         cached_input_per_1k=_FAKE_CACHED,
                         output_per_1k=_FAKE_OUTPUT)
        for f in families
    ])
    result = pricing.estimate_frame(frame, fake)
    priced = result["pricing_status"] == pricing.STATUS_PRICED
    lines.append(f"  priced {_fmt_int(priced.sum())} 筆 "
                 f"（{priced.sum() / len(frame):.2%}），其餘為本地或非 token 端點")

    expected = (result["uncached_prompt_tokens"] / 1000 * _FAKE_INPUT
                + result["cached_tokens"] / 1000 * _FAKE_CACHED
                + result["completion_tokens"] / 1000 * _FAKE_OUTPUT)
    diff = (result.loc[priced, "cost"] - expected[priced]).abs()

    cached_hit = priced & (frame["usage.cached_tokens"].fillna(0) > 0)
    checks = [
        ("每一筆 priced 的總額都等於三段獨立重算的結果", bool(diff.lt(1e-9).all())),
        ("有快取命中的列，快取段金額 > 0",
         bool((result.loc[cached_hit, "cached_cost"] > 0).all())),
        ("無快取命中的列，快取段金額為 0",
         bool((result.loc[priced & ~cached_hit, "cached_cost"] == 0).all())),
        ("金額為 0 的 priced 列，token 也是 0",
         bool((result.loc[priced & (result["cost"] == 0),
                          ["prompt_tokens", "completion_tokens"]].sum(axis=1) == 0).all())),
    ]
    for label, passed in checks:
        lines.append(f"  {'ok  ' if passed else 'FAIL'} {label}")

    # 手算一筆對照，數字直接寫在報告裡，不用回頭翻程式碼。
    hit = frame.index[cached_hit]
    if len(hit):
        idx = result.loc[hit, "cost"].idxmax()
        r = result.loc[idx]
        manual = (r["uncached_prompt_tokens"] / 1000 * _FAKE_INPUT
                  + r["cached_tokens"] / 1000 * _FAKE_CACHED
                  + r["completion_tokens"] / 1000 * _FAKE_OUTPUT)
        lines.append(
            f"  手算對照：uncached {_fmt_int(r['uncached_prompt_tokens'])}/1000*1.0"
            f" + cached {_fmt_int(r['cached_tokens'])}/1000*0.1"
            f" + out {_fmt_int(r['completion_tokens'])}/1000*2.0"
            f" = {manual:.6f}，函式回 {r['cost']:.6f}"
            f"（{'相符' if abs(manual - r['cost']) < 1e-9 else '不符'}）")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lite_index", type=Path,
                        help="lite_index.parquet 的路徑")
    parser.add_argument("--pricing-table", type=Path, default=None,
                        help=f"價目表路徑，預設 {pricing.PRICING_TABLE_PATH}")
    args = parser.parse_args(argv)

    if not args.lite_index.is_file():
        print(f"找不到 {args.lite_index}", file=sys.stderr)
        return 2

    table = pricing.PricingTable.from_csv(args.pricing_table)
    print(f"價目表 {len(table)} 列，"
          f"其中填了價格的 model_family {len(table.priced_families)} 個"
          f"（共 {len(table.families)} 個）")

    frame = pd.read_parquet(args.lite_index)
    result = pricing.estimate_frame(frame, table)

    lines = build_report(frame, result)
    if not table.priced_families:
        lines += arithmetic_check(frame)
    print("\n".join(lines))

    # 價目表沒填價時，覆蓋率必然是 0，不該因此讓檢查失敗。
    return 1 if any(line.startswith("!!") or " FAIL " in line for line in lines) else 0


if __name__ == "__main__":
    raise SystemExit(main())
