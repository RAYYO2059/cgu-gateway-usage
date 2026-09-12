"""分流層結案檔的存在、證據邊界與本機資料勾稽。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
LIMITS = REPO / "docs" / "CLASSIFICATION_LIMITS.md"


def test限制檔含六段證據與指標欄位():
    text = LIMITS.read_text(encoding="utf-8")
    for heading in (
        "## A. 兩模型的交叉一致率", "## B. 91.7% 不是中繼資料上的一致",
        "## C. 機械不可分證明", "## D. 預先寫定的判準從頭沒有作用",
        "## E. 人工標註只使用兩個值", "## F. 結論與交付含意",
    ):
        assert heading in text
    for column in ("回答什麼", "單位", "來源表", "分母", "覆蓋率", "注意事項", "版本"):
        assert column in text
    assert "53.4%" in text and "31/58" in text
    assert "13,646" in text and "48,893" in text
    assert "不進報告結論" in text


def test本機限制數字與安全聚合相符():
    from src.classify_lite.summarize_classification_limits import build_summary

    try:
        summary = build_summary()
    except FileNotFoundError:
        pytest.skip("repo 外分流工作檔不在本機")
    cross = summary["cross_judgement"]
    assert (cross["disposition_agree"], cross["n"]) == (31, 58)
    assert cross["per_opus_class"] == {
        "tool_injected": {"agree": 11, "denominator": 12},
        "user_envelope": {"agree": 9, "denominator": 15},
        "service_relay": {"agree": 4, "denominator": 10},
        "batch_project": {"agree": 7, "denominator": 20},
        "insufficient": {"agree": 0, "denominator": 1},
    }
    assert summary["spread"]["both"]["triple_1_1_1"] == 3
    assert (summary["spread"]["one_model"]["triple_1_1_1"]
            + summary["spread"]["neither"]["triple_1_1_1"]) == 17
    assert summary["ray_30"]["models_same_ray_different"] == 10
    assert summary["prefilter"]["distinct_contents"] == 13_646
    assert summary["prefilter"]["requests"] == 48_893
