"""只輸出 domain 內容上限校準的安全聚合，不輸出內容或逐筆判定。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
SAMPLE = SCRATCHPAD / "domain_cap_calibration_sample.csv"
OUTPUT_1200 = SCRATCHPAD / "domain_cap_calibration.1200.csv"
OUTPUT_4000 = SCRATCHPAD / "domain_cap_calibration.4000.csv"


def summarize(sample: pd.DataFrame, at_1200: pd.DataFrame,
              at_4000: pd.DataFrame) -> dict[str, object]:
    """比較相同 sha256 的兩側結果；回傳值不含 sha256 或任何逐筆欄位。"""
    key = "prompt_text_sha256"
    expected = set(sample[key])
    if len(sample) != 50 or sample[key].nunique() != 50:
        raise ValueError("校準樣本不是 50 個不重複 sha256")
    for rows, cap in ((at_1200, 1200), (at_4000, 4000)):
        if len(rows) != 50 or rows[key].nunique() != 50 or set(rows[key]) != expected:
            raise ValueError(f"{cap} 輸出未完整覆蓋校準樣本")

    left = at_1200.set_index(key)
    right = at_4000.set_index(key)
    meta = sample.set_index(key)
    changed = left["domain"] != right["domain"]
    truncated = meta["truncated_at_1200"].astype(str).str.lower().eq("true")
    cost_1200 = float(pd.to_numeric(left["total_cost_usd"]).sum())
    cost_4000 = float(pd.to_numeric(right["total_cost_usd"]).sum())
    tool_events = int(
        (~left["tool_events"].isin(("", "[]"))).sum()
+        (~right["tool_events"].isin(("", "[]"))).sum())
    return {
        "n": 50,
        "domain_changed": int(changed.sum()),
        "truncated_at_1200": {
            "n": int(truncated.sum()),
            "domain_changed": int(changed[truncated].sum()),
        },
        "not_truncated_at_1200": {
            "n": int((~truncated).sum()),
            "domain_changed": int(changed[~truncated].sum()),
        },
        "still_truncated_at_4000": int((meta["prompt_text_len"].astype(int) > 4000).sum()),
        "estimated_cost_usd": {
            "cap_1200": round(cost_1200, 6),
            "cap_4000": round(cost_4000, 6),
            "difference": round(cost_4000 - cost_1200, 6),
            "total": round(cost_1200 + cost_4000, 6),
        },
        "external_tool_events": tool_events,
    }


def main() -> None:
    result = summarize(
        pd.read_csv(SAMPLE, encoding="utf-8-sig", dtype=str, keep_default_na=False),
        pd.read_csv(OUTPUT_1200, encoding="utf-8-sig", dtype=str, keep_default_na=False),
        pd.read_csv(OUTPUT_4000, encoding="utf-8-sig", dtype=str, keep_default_na=False),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
