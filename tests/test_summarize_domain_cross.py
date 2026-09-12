"""Domain 兩模型彙總固定分母與安全輸出的測試。"""

from __future__ import annotations

import json

import pandas as pd

from src.classify_lite import summarize_domain_cross as summary


def _sample() -> pd.DataFrame:
    return pd.DataFrame({
        "prompt_text_sha256": [f"{i:064x}" for i in range(600)],
        "prompt_text_len": [1300] * 281 + [100] * 319,
        "n_requests": [2] * 600,
        "sample_weight": [1.0] * 600,
    })


def _output(model: str) -> pd.DataFrame:
    return pd.DataFrame({
        "prompt_text_sha256": [f"{i:064x}" for i in range(600)],
        "domain": ["software"] * 300 + ["research"] * 300,
        "named_third_party": ["true"] * 5 + ["false"] * 595,
        "confidence": ["high"] * 500 + ["low"] * 100,
        "model": [model] * 600,
        "prompt_sha256": [f"{1 if model == 'opus' else 2:064x}"] * 600,
        "tool_events": ["[]"] * 600,
        "total_cost_usd": ["0.01"] * 600,
        "prompt_tokens_total": ["100"] * 600,
    })


def test固定600分母且工具事件算不一致():
    opus = _output("opus")
    codex = _output("codex")
    codex.loc[0, "tool_events"] = '[{"type":"Read","count":1}]'
    codex.loc[1:10, "domain"] = "general"
    result = summary.summarize(_sample(), opus, codex)
    assert result["cross"]["denominator"] == 600
    assert result["cross"]["agree"] == 589
    assert result["codex"]["tool_events"] == 1
    assert result["truncated_at_1200"] == 281
    assert result["opus"]["domain"][2]["distinct_contents"] == 300
    assert result["opus"]["domain"][2]["requests"] == 600
    assert result["cross"]["named_third_party_intersection"] == 5
    assert "prompt_text_sha256" not in json.dumps(result)


def test缺列留在分母並計為失敗():
    opus = _output("opus").iloc[:590].copy()
    codex = _output("codex").iloc[:580].copy()
    result = summary.summarize(_sample(), opus, codex)
    assert result["opus"]["failures"] == 10
    assert result["codex"]["failures"] == 20
    assert result["cross"]["denominator"] == 600
    assert result["cross"]["agree"] == 580
