"""分層估計與第三人審查清單的人工合成資料測試。"""

from __future__ import annotations

import csv
import json
import math

import pandas as pd

from src.classify_lite import analyze_domain_population as analysis


def _synthetic():
    keys = [f"synthetic_{i:03d}" for i in range(600)]
    sample = pd.DataFrame({
        "prompt_text_sha256": keys,
        "source_path": [f"local_{i:03d}" for i in range(600)],
        "prompt_text_len": [100] * 600,
        "unit_type": ["first"] * 300 + ["second"] * 300,
        "length_stratum": ["Q1"] * 600,
        "n_requests": [10] * 300 + [2] * 150 + [1] * 150,
        "stratum_population": [300] * 300 + [17065] * 300,
        "stratum_sample_size": [300] * 600,
        "sample_weight": [1] * 300 + [17065 / 300] * 300,
    })
    opus = pd.DataFrame({
        "prompt_text_sha256": keys,
        "domain": ["clinical"] * 450 + ["unknown"] * 150,
        "named_third_party": ["false"] * 600,
    })
    codex = opus.copy()
    return sample, opus, codex


def test_stratified_fpc_and_request_ratio():
    sample, opus, _ = _synthetic()
    result = analysis.stratified_estimates(sample, opus)
    clinical = next(row for row in result["domains"] if row["domain"] == "clinical")
    content_share = (300 + 17065 / 2) / 17365
    request_share = (3000 + 17065) / (3000 + 17065 + 17065 / 2)
    assert math.isclose(clinical["content_share"], content_share)
    assert math.isclose(clinical["request_share"], request_share)
    assert not math.isclose(content_share, clinical["sample_content_share"])
    binary_sample_variance = 150 / 299 * 0.5
    variance = ((17065 / 17365) ** 2 * (1 - 300 / 17065)
                * binary_sample_variance / 300)
    assert math.isclose(clinical["content_ci95"][1] - content_share,
                        analysis.Z_95 * math.sqrt(variance))
    assert result["weighted_request_total"] != result["actual_request_total"]
    assert "not_applicable" in result["below_min_group_size"]
    rare = next(row for row in result["domains"] if row["domain"] == "not_applicable")
    assert rare["content_share"] is None and rare["request_share"] is None


def test_review_list_is_union_and_only_two_allowed_columns(tmp_path):
    sample, opus, codex = _synthetic()
    opus.loc[0, "named_third_party"] = "true"
    codex.loc[0, "named_third_party"] = "true"
    codex.loc[1, "named_third_party"] = "true"
    path = tmp_path / "review.csv"
    assert analysis.write_review_list(sample, opus, codex, path) == 2
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == list(analysis.REVIEW_COLUMNS)
    assert {row["prompt_text_sha256"] for row in rows} == {
        "synthetic_000", "synthetic_001"}
    assert all(len(row) == 2 for row in rows)


def test_not_applicable_metadata_exposes_no_sha_uid_or_path(tmp_path, monkeypatch):
    sample, opus, codex = _synthetic()
    sample.loc[:12, "n_requests"] = 1
    opus.loc[:12, "domain"] = "not_applicable"
    codex.loc[:8, "domain"] = "not_applicable"
    keys = sample.loc[:12, "prompt_text_sha256"].tolist()
    frame = pd.DataFrame({
        "prompt_text_sha256": keys,
        "anonymous_user_id": [f"private_uid_{i}" for i in range(13)],
        "provider": ["openai"] * 13,
        "request_style": ["responses"] * 13,
        "model_returned": ["model"] * 13,
    })
    users = pd.DataFrame({"anonymous_user_id": frame["anonymous_user_id"],
                          "unit_type": ["first"] * 13})
    members = pd.DataFrame({"prompt_text_sha256": keys,
                            "group_id": ["A001"] * 13})
    users_path, members_path = tmp_path / "users.parquet", tmp_path / "members.parquet"
    users.to_parquet(users_path, index=False)
    members.to_parquet(members_path, index=False)
    monkeypatch.setattr(analysis.extract_lite, "load_dataset", lambda: frame)
    monkeypatch.setattr(analysis.aggregate_lite, "USER_LITE_PATH", users_path)
    monkeypatch.setattr(analysis, "MEMBERS", members_path)
    result = analysis.not_applicable_metadata(sample, opus, codex)
    rendered = json.dumps(result)
    assert result["in_clusters"] == 13 and result["scattered"] == 0
    assert all(key not in rendered for key in keys)
    assert "private_uid_" not in rendered and "local_" not in rendered
