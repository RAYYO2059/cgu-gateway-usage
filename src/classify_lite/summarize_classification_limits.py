"""重算分流層結案所需的安全聚合數字。

只讀既有判定、盲標與群層中繼資料；不讀或輸出任何前綴／prompt_text／reason。
輸出是可直接核對 ``docs/CLASSIFICATION_LIMITS.md`` 的 JSON。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
CODEX_ROOT = PROJECT_ROOT.parents[1] / "codex_blind_2026-09"
OPUS_OUTPUT = SCRATCHPAD / "judge_output.e13f9738.csv"
CODEX_OUTPUT = CODEX_ROOT / "codex_judge_output.csv"
BLIND_SAMPLE = SCRATCHPAD / "blind_label_sample.csv"
RAY_LABELS = SCRATCHPAD / "blind_labels.csv"
CLUSTERS = SCRATCHPAD / "clusters.parquet"
MEMBERS = SCRATCHPAD / "cluster_members.parquet"
PREFILTER_COUNTS = (
    PROJECT_ROOT / "runs" / "2026-09-05T1700_prefilter"
    / "classify_lite" / "prefilter_counts.csv"
)


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str,
                       keep_default_na=False)


def _win_rate(left: pd.Series, right: pd.Series) -> float:
    """P(left > right) + 0.5 P(left == right)，用全部跨組配對。"""
    wins = ties = 0
    for a in left.astype(float):
        for b in right.astype(float):
            wins += a > b
            ties += a == b
    return (wins + 0.5 * ties) / (len(left) * len(right))


def build_summary() -> dict[str, object]:
    sample = _read_csv(BLIND_SAMPLE)[["group_id"]]
    opus = _read_csv(OPUS_OUTPUT)
    codex = _read_csv(CODEX_OUTPUT)
    if codex["tool_events"].ne("[]").any():
        raise ValueError("Codex 交叉判定含工具事件，不能併入一致率")
    joined = (
        sample.merge(opus, on="group_id", validate="one_to_one")
        .merge(codex, on="group_id", suffixes=("_opus", "_codex"),
               validate="one_to_one")
    )
    if len(joined) != 58:
        raise ValueError(f"交叉判定分母應為 58，實際 {len(joined)}")

    disposition_order = [
        "tool_injected", "user_envelope", "service_relay",
        "batch_project", "insufficient",
    ]
    per_class: dict[str, dict[str, int]] = {}
    for label in disposition_order:
        rows = joined[joined["disposition_opus"] == label]
        per_class[label] = {
            "agree": int((rows["disposition_codex"] == label).sum()),
            "denominator": int(len(rows)),
        }

    clusters = pd.read_parquet(CLUSTERS)
    members = pd.read_parquet(
        MEMBERS, columns=["group_id", "unit_type", "model_returned"])
    kinds = members.groupby("group_id").agg(
        unit_type種類=("unit_type", "nunique"),
        model種類=("model_returned", "nunique"),
    ).reset_index()
    metadata = clusters[["group_id", "uid數"]].merge(
        kinds, on="group_id", how="left", validate="one_to_one")
    joined = joined.merge(metadata, on="group_id", validate="one_to_one")
    opus_tool = joined["disposition_opus"] == "tool_injected"
    codex_tool = joined["disposition_codex"] == "tool_injected"
    joined["tool_group"] = "neither"
    joined.loc[opus_tool ^ codex_tool, "tool_group"] = "one_model"
    joined.loc[opus_tool & codex_tool, "tool_group"] = "both"

    spread: dict[str, dict[str, object]] = {}
    for group in ("both", "one_model", "neither"):
        rows = joined[joined["tool_group"] == group]
        spread[group] = {
            "n": int(len(rows)),
            "uid_median": float(rows["uid數"].median()),
            "unit_type_median": float(rows["unit_type種類"].median()),
            "model_median": float(rows["model種類"].median()),
            "triple_1_1_1": int(((rows["uid數"] == 1)
                                  & (rows["unit_type種類"] == 1)
                                  & (rows["model種類"] == 1)).sum()),
        }
    spread["both"]["uid_win_rate_vs_one_model"] = _win_rate(
        joined.loc[joined["tool_group"] == "both", "uid數"],
        joined.loc[joined["tool_group"] == "one_model", "uid數"],
    )

    ray = _read_csv(RAY_LABELS)
    tri = joined.merge(
        ray[["group_id", "frame_owner", "disposition"]], on="group_id",
        suffixes=("", "_ray"), validate="one_to_one",
    )
    models_same = (
        (tri["frame_owner_opus"] == tri["frame_owner_codex"])
        & (tri["disposition_opus"] == tri["disposition_codex"])
    )
    opus_ray_same = (
        (tri["frame_owner_opus"] == tri["frame_owner"])
        & (tri["disposition_opus"] == tri["disposition"])
    )
    codex_ray_same = (
        (tri["frame_owner_codex"] == tri["frame_owner"])
        & (tri["disposition_codex"] == tri["disposition"])
    )
    models_same_ray_diff = tri[models_same & ~opus_ray_same]

    all_opus = _read_csv(OPUS_OUTPUT)
    current_tool = all_opus[all_opus["disposition"] == "tool_injected"]
    a001_runs = {}
    for filename in (
        "judge_output.csv", "judge_output.a0d19daa.rerun1.csv",
        "judge_output.e13f9738.csv",
    ):
        row = _read_csv(SCRATCHPAD / filename)
        value = row.loc[row["group_id"] == "A001", "disposition"]
        a001_runs[filename] = value.iloc[0] if len(value) else None

    prefilter = pd.read_csv(PREFILTER_COUNTS, encoding="utf-8-sig")
    ruled = prefilter[prefilter["rule"] != "需送分類器"]
    manifest = json.loads((CODEX_ROOT / "prompt_manifest.json").read_text(
        encoding="utf-8"))

    return {
        "cross_judgement": {
            "n": len(joined),
            "disposition_agree": int((
                joined["disposition_opus"] == joined["disposition_codex"]
            ).sum()),
            "per_opus_class": per_class,
            "opus_model": sorted(joined["model_opus"].unique()),
            "opus_fingerprint": sorted(joined["prompt_sha256_opus"].unique()),
            "codex_model": sorted(joined["model_codex"].unique()),
            "codex_execution_fingerprint": sorted(
                joined["prompt_sha256_codex"].unique()),
            "codex_prompt_content_fingerprint": manifest[
                "codex_prompt_content_fingerprint"],
            "prompt_difference_statement": manifest["difference_statement"],
            "opus_judged_at_min": joined["judged_at_opus"].min(),
            "opus_judged_at_max": joined["judged_at_opus"].max(),
            "codex_judged_at_min": joined["judged_at_codex"].min(),
            "codex_judged_at_max": joined["judged_at_codex"].max(),
        },
        "spread": spread,
        "current_225": {
            "tool_injected_n": int(len(current_tool)),
            "tool_injected_uid_median": float(current_tool.merge(
                metadata, on="group_id")["uid數"].median()),
            "tool_injected_unit_type_median": float(current_tool.merge(
                metadata, on="group_id")["unit_type種類"].median()),
            "a001_uid_n": int(metadata.loc[
                metadata["group_id"] == "A001", "uid數"].iloc[0]),
            "a001_three_runs": a001_runs,
        },
        "ray_30": {
            "n": len(tri),
            "disposition_counts": tri["disposition"].value_counts().to_dict(),
            "three_way_same": int((models_same & opus_ray_same).sum()),
            "models_same_ray_different": int(len(models_same_ray_diff)),
            "models_same_ray_different_ray_values": (
                models_same_ray_diff["disposition"].value_counts().to_dict()),
            "models_different": int((~models_same).sum()),
            "opus_ray_same": int(opus_ray_same.sum()),
            "codex_ray_same": int(codex_ray_same.sum()),
        },
        "prefilter": {
            "distinct_contents": int(ruled["相異內容"].sum()),
            "requests": int(ruled["請求數"].sum()),
            "rows": ruled.to_dict("records"),
        },
    }


def main() -> None:
    print(json.dumps(build_summary(), ensure_ascii=False, indent=2,
                     sort_keys=True))


if __name__ == "__main__":
    main()
