"""既有 domain 樣本的分層估計與安全中繼資料盤點；不讀內容明文。"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pandas as pd

from src import aggregate_lite, extract_lite
from src.classify_lite import judge_domain, summarize_domain_cross


ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = ROOT.parent / "_rescued_scratchpad"
SAMPLE = SCRATCHPAD / "domain_sample.csv"
OPUS = SCRATCHPAD / "judge_domain_output.csv"
CODEX = ROOT.parents[1] / "codex_domain_blind_2026-09" / "codex_domain_output.csv"
MEMBERS = SCRATCHPAD / "cluster_members.parquet"
REVIEW_LIST = SCRATCHPAD / "domain_named_third_party_union.csv"
POPULATION_N = 17_365
POPULATION_REQUESTS = 58_100
Z_95 = 1.959963984540054
REVIEW_COLUMNS = ("prompt_text_sha256", "source_path")


def _csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)


def _interval(estimate: float, variance: float) -> tuple[float, float]:
    half = Z_95 * math.sqrt(max(variance, 0.0))
    return max(0.0, estimate - half), min(1.0, estimate + half)


def stratified_estimates(sample: pd.DataFrame, opus: pd.DataFrame) -> dict:
    """SRSWOR 分層率與線性化請求比值率，含有限母體修正。"""
    key = REVIEW_COLUMNS[0]
    if len(sample) != 600 or sample[key].nunique() != 600:
        raise ValueError("domain 樣本須有 600 個不重複 sha256")
    if len(opus) != 600 or opus[key].nunique() != 600 or set(opus[key]) != set(sample[key]):
        raise ValueError("Opus 判定未完整對齊固定 600 筆樣本")
    if set(opus["domain"]) - set(judge_domain.DOMAIN_VALUES):
        raise ValueError("domain 超出既有值域")
    joined = sample.merge(opus[[key, "domain"]], on=key, validate="one_to_one")
    for col in ("n_requests", "stratum_population", "stratum_sample_size"):
        joined[col] = pd.to_numeric(joined[col], errors="raise").astype(int)
    joined["stratum"] = joined["unit_type"] + "|" + joined["length_stratum"]

    strata = []
    for name, part in joined.groupby("stratum", sort=True):
        N = int(part["stratum_population"].iloc[0])
        n = len(part)
        if (part["stratum_population"] != N).any() or (part["stratum_sample_size"] != n).any():
            raise ValueError(f"{name} 的母體數或樣本數不一致")
        if not 2 <= n <= N:
            raise ValueError(f"{name} 的抽樣數不足或超過母體")
        if not pd.to_numeric(part["sample_weight"]).between(N / n - 1e-9, N / n + 1e-9).all():
            raise ValueError(f"{name} 的檔案權重與 N/n 不一致")
        strata.append((name, N, n, part))
    if sum(N for _, N, _, _ in strata) != POPULATION_N or sum(n for _, _, n, _ in strata) != 600:
        raise ValueError("分層母體或樣本未對齊凍結數")

    weighted_request_total = sum(N * part["n_requests"].mean() for _, N, _, part in strata)
    if weighted_request_total <= 0:
        raise ValueError("加權請求分母為零")
    out = []
    for domain in judge_domain.DOMAIN_VALUES:
        observed_n = int((joined["domain"] == domain).sum())
        estimated_contents = sum(N * (part["domain"] == domain).mean()
                                 for _, N, _, part in strata)
        p_content = estimated_contents / POPULATION_N
        var_content = sum(
            (N / POPULATION_N) ** 2 * (1 - n / N)
            * (part["domain"] == domain).astype(float).var(ddof=1) / n
            for _, N, n, part in strata
        )
        estimated_requests = sum(
            N * (part["n_requests"] * (part["domain"] == domain)).mean()
            for _, N, _, part in strata
        )
        p_request = estimated_requests / weighted_request_total
        var_request = sum(
            N ** 2 * (1 - n / N)
            * (part["n_requests"] * ((part["domain"] == domain).astype(float)
                                      - p_request)).var(ddof=1) / n
            for _, N, n, part in strata
        ) / weighted_request_total ** 2
        out.append({
            "domain": domain,
            "sample_n": observed_n,
            "sample_content_share": observed_n / 600,
            "sample_requests": int(joined.loc[joined["domain"] == domain, "n_requests"].sum()),
            "estimated_contents": estimated_contents,
            "content_share": p_content if observed_n >= 10 else None,
            "content_ci95": _interval(p_content, var_content) if observed_n >= 10 else None,
            "estimated_requests": estimated_requests,
            "request_share": p_request if observed_n >= 10 else None,
            "request_ci95": _interval(p_request, var_request) if observed_n >= 10 else None,
        })
    return {
        "strata": [{"stratum": name, "population": N, "sample": n, "weight": N / n}
                   for name, N, n, _ in strata],
        "weighted_request_total": weighted_request_total,
        "actual_request_total": POPULATION_REQUESTS,
        "domains": out,
        "below_min_group_size": [x["domain"] for x in out if x["sample_n"] < 10],
    }


def write_review_list(sample: pd.DataFrame, opus: pd.DataFrame,
                      codex: pd.DataFrame, path: Path = REVIEW_LIST) -> int:
    """只輸出兩模型第三人標記聯集的 sha256 與樣本既有來源路徑。"""
    key = REVIEW_COLUMNS[0]
    for label, rows in (("Opus", opus), ("Codex", codex)):
        if len(rows) != 600 or rows[key].nunique() != 600 or set(rows[key]) != set(sample[key]):
            raise ValueError(f"{label} 判定未完整對齊固定樣本")
        if not rows["named_third_party"].str.lower().isin(("true", "false")).all():
            raise ValueError(f"{label} 第三人標記非布林值")
    selected = (set(opus.loc[opus["named_third_party"].str.lower() == "true", key])
                | set(codex.loc[codex["named_third_party"].str.lower() == "true", key]))
    index = sample.set_index(key)
    if index.index.has_duplicates or index.loc[list(selected), "source_path"].eq("").any():
        raise ValueError("樣本鍵重複或來源路徑缺漏")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(REVIEW_COLUMNS)
        for sha in sorted(selected):
            writer.writerow((sha, index.at[sha, "source_path"]))
    return len(selected)


def not_applicable_metadata(sample: pd.DataFrame, opus: pd.DataFrame,
                            codex: pd.DataFrame) -> dict:
    """只讀非明文中繼資料，逐筆輸出匿名序號，絕不輸出 uid/sha/path。"""
    key = REVIEW_COLUMNS[0]
    ids = set(opus.loc[opus["domain"] == "not_applicable", key])
    if len(ids) != 13:
        raise ValueError("not_applicable 筆數與已審結果不符")
    meta = sample.set_index(key)
    right = codex.set_index(key)
    frame = extract_lite.load_dataset()
    if "prompt_text" in frame.columns:
        raise AssertionError("請求資料含明文欄，停止盤點")
    cols = [key, "anonymous_user_id", "provider", "request_style", "model_returned"]
    selected = frame.loc[frame[key].isin(ids), cols].copy()
    users = pd.read_parquet(aggregate_lite.USER_LITE_PATH,
                            columns=["anonymous_user_id", "unit_type"])
    selected = selected.merge(users, on="anonymous_user_id", how="left", validate="many_to_one")
    if selected["unit_type"].isna().any():
        raise ValueError("unit_type 對接缺漏")
    members = pd.read_parquet(MEMBERS, columns=[key, "group_id"])
    if members[key].duplicated().any():
        raise ValueError("群成員不是每個相異內容一列")
    member_map = members.set_index(key)["group_id"].to_dict()
    cases = []
    for i, sha in enumerate(sorted(ids), start=1):
        rows = selected[selected[key] == sha]
        if len(rows) != int(meta.at[sha, "n_requests"]):
            raise ValueError("請求重複次數與抽樣檔不一致")
        cases.append({
            "case": f"case_{i:02d}",
            "cross": "both" if right.at[sha, "domain"] == "not_applicable" else "opus_only",
            "length": int(meta.at[sha, "prompt_text_len"]),
            "requests": len(rows),
            "uid_count": int(rows["anonymous_user_id"].nunique()),
            "unit_types": sorted(rows["unit_type"].dropna().unique().tolist()),
            "providers": sorted(rows["provider"].dropna().astype(str).unique().tolist()),
            "request_styles": sorted(rows["request_style"].dropna().astype(str).unique().tolist()),
            "models": sorted(rows["model_returned"].dropna().astype(str).unique().tolist()),
            "group_id": member_map.get(sha, ""),
        })
    return {
        "cases": cases,
        "in_clusters": sum(bool(x["group_id"]) for x in cases),
        "scattered": sum(not x["group_id"] for x in cases),
        "group_counts": {gid: sum(x["group_id"] == gid for x in cases)
                         for gid in sorted({x["group_id"] for x in cases if x["group_id"]})},
        "uid_count_all": int(selected["anonymous_user_id"].nunique()),
    }


def main() -> None:
    sample, opus, codex = _csv(SAMPLE), _csv(OPUS), _csv(CODEX)
    summary = summarize_domain_cross.summarize(sample, opus, codex)
    if summary["opus"]["tool_events"] or summary["codex"]["tool_events"]:
        raise ValueError("含工具事件，不併入分析")
    estimates = stratified_estimates(sample, opus)
    union_n = write_review_list(sample, opus, codex)
    metadata = not_applicable_metadata(sample, opus, codex)
    print(json.dumps({
        "estimates": estimates,
        "not_applicable_metadata": metadata,
        "named_third_party_union_n": union_n,
        "named_third_party_list": str(REVIEW_LIST),
    }, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
