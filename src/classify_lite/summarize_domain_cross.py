"""彙總 Opus/Codex 的 600 筆 domain 結果；不輸出 sha256 或逐筆資料。"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import pandas as pd

from src.classify_lite import judge_domain as opus
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
OPUS_OUTPUT = SCRATCHPAD / "judge_domain_output.csv"
SAMPLE = SCRATCHPAD / "domain_sample.csv"
CODEX_OUTPUT = PROJECT_ROOT.parents[1] / "codex_domain_blind_2026-09" / "codex_domain_output.csv"
EXPECTED_N = 600
AGREEMENT_GATE = 0.75


def _event_count(value: str) -> int:
    parsed = json.loads(value or "[]")
    if not isinstance(parsed, list):
        raise ValueError("tool_events 不是陣列")
    total = 0
    for item in parsed:
        total += int(item.get("count", 1)) if isinstance(item, dict) else 1
    return total


def _load(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)


def summarize(sample: pd.DataFrame, opus_rows: pd.DataFrame,
              codex_rows: pd.DataFrame) -> dict[str, object]:
    key = "prompt_text_sha256"
    if len(sample) != EXPECTED_N or sample[key].nunique() != EXPECTED_N:
        raise ValueError("domain 樣本不是 600 個不重複 sha256")
    eligible = set(sample[key])
    for name, rows in (("opus", opus_rows), ("codex", codex_rows)):
        if not rows.empty:
            if rows[key].duplicated().any() or not set(rows[key]) <= eligible:
                raise ValueError(f"{name} 輸出有重複或樣本外 sha256")
            if not set(rows["domain"]) <= set(opus.DOMAIN_VALUES):
                raise ValueError(f"{name} domain 超出值域")

    meta = sample.set_index(key)
    left = opus_rows.set_index(key) if not opus_rows.empty else pd.DataFrame()
    right = codex_rows.set_index(key) if not codex_rows.empty else pd.DataFrame()
    domain_rows = []
    for domain in opus.DOMAIN_VALUES:
        subset = left[left["domain"] == domain] if not left.empty else left
        ids = subset.index if not subset.empty else []
        domain_rows.append({
            "domain": domain,
            "distinct_contents": int(len(subset)),
            "requests": int(meta.loc[ids, "n_requests"].astype(int).sum())
            if len(ids) else 0,
            "confidence": {
                level: int((subset["confidence"] == level).sum())
                for level in opus.CONFIDENCE_VALUES
            } if not subset.empty else {level: 0 for level in opus.CONFIDENCE_VALUES},
        })

    opus_tools = sum(_event_count(value) for value in opus_rows.get(
        "tool_events", pd.Series(dtype=str)))
    codex_tools = sum(_event_count(value) for value in codex_rows.get(
        "tool_events", pd.Series(dtype=str)))
    opus_event_by_id = ({idx: _event_count(value) for idx, value in
                         left["tool_events"].items()} if not left.empty else {})
    codex_event_by_id = ({idx: _event_count(value) for idx, value in
                          right["tool_events"].items()} if not right.empty else {})

    matched = 0
    confusion: Counter[tuple[str, str]] = Counter()
    mismatch_pairs: Counter[tuple[str, str]] = Counter()
    for sha in meta.index:
        l_present = not left.empty and sha in left.index
        r_present = not right.empty and sha in right.index
        l_domain = str(left.at[sha, "domain"]) if l_present else "__failure__"
        if not r_present:
            r_domain = "__failure__"
        elif codex_event_by_id.get(sha, 0):
            r_domain = "__tool_event__"
        else:
            r_domain = str(right.at[sha, "domain"])
        confusion[(l_domain, r_domain)] += 1
        clean = (l_present and r_present and not opus_event_by_id.get(sha, 0)
                 and not codex_event_by_id.get(sha, 0))
        if clean and l_domain == r_domain:
            matched += 1
        elif clean:
            mismatch_pairs[(l_domain, r_domain)] += 1

    per_opus = []
    for domain in opus.DOMAIN_VALUES:
        denominator = int((left["domain"] == domain).sum()) if not left.empty else 0
        agree = 0
        if denominator:
            for sha in left.index[left["domain"] == domain]:
                if (sha in right.index and not opus_event_by_id.get(sha, 0)
                        and not codex_event_by_id.get(sha, 0)
                        and right.at[sha, "domain"] == domain):
                    agree += 1
        per_opus.append({"domain": domain, "agree": agree,
                         "denominator": denominator})

    left_true = (set(left.index[left["named_third_party"].str.lower() == "true"])
                 if not left.empty else set())
    right_true = (set(right.index[right["named_third_party"].str.lower() == "true"])
                  if not right.empty else set())
    agreement = matched / EXPECTED_N
    result: dict[str, object] = {
        "sample_n": EXPECTED_N,
        "truncated_at_1200": int((meta["prompt_text_len"].astype(int) > 1200).sum()),
        "opus": {
            "completed": int(len(opus_rows)),
            "failures": EXPECTED_N - int(len(opus_rows)),
            "model": sorted(set(opus_rows.get("model", []))),
            "fingerprint": sorted(set(opus_rows.get("prompt_sha256", []))),
            "domain": domain_rows,
            "confidence": {level: int((opus_rows["confidence"] == level).sum())
                           for level in opus.CONFIDENCE_VALUES}
            if not opus_rows.empty else {level: 0 for level in opus.CONFIDENCE_VALUES},
            "named_third_party_true": len(left_true),
            "tool_events": opus_tools,
            "estimated_cost_usd": round(float(pd.to_numeric(
                opus_rows.get("total_cost_usd", pd.Series(dtype=float))).sum()), 6),
            "prompt_tokens_total_min": (None if opus_rows.empty else int(pd.to_numeric(
                opus_rows["prompt_tokens_total"], errors="coerce").min())),
            "prompt_tokens_total_missing": (EXPECTED_N if opus_rows.empty else int(
                pd.to_numeric(opus_rows["prompt_tokens_total"], errors="coerce").isna().sum())),
        },
        "codex": {
            "completed": int(len(codex_rows)),
            "failures": EXPECTED_N - int(len(codex_rows)),
            "model": sorted(set(codex_rows.get("model", []))),
            "fingerprint": sorted(set(codex_rows.get("prompt_sha256", []))),
            "named_third_party_true": len(right_true),
            "tool_events": codex_tools,
        },
        "cross": {
            "agree": matched,
            "denominator": EXPECTED_N,
            "agreement": agreement,
            "passes_0_75": agreement >= AGREEMENT_GATE,
            "per_opus_domain": per_opus,
            "confusion": [
                {"opus": pair[0], "codex": pair[1], "n": count}
                for pair, count in sorted(confusion.items())
            ],
            "top_mismatch_pairs": [
                {"opus": pair[0], "codex": pair[1], "n": count}
                for pair, count in mismatch_pairs.most_common()
            ],
            "named_third_party_intersection": len(left_true & right_true),
        },
    }
    if agreement >= AGREEMENT_GATE and not left.empty:
        weights = pd.to_numeric(meta["sample_weight"])
        estimates = []
        for domain in opus.DOMAIN_VALUES:
            ids = left.index[left["domain"] == domain]
            estimated_contents = float(weights.loc[ids].sum()) if len(ids) else 0.0
            estimated_requests = float((
                weights.loc[ids] * meta.loc[ids, "n_requests"].astype(float)).sum()) \
                if len(ids) else 0.0
            estimates.append({
                "domain": domain,
                "estimated_distinct_contents": round(estimated_contents, 2),
                "estimated_requests": round(estimated_requests, 2),
            })
        result["weighted_estimates"] = estimates
    return result


def main() -> None:
    result = summarize(_load(SAMPLE), _load(OPUS_OUTPUT), _load(CODEX_OUTPUT))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
