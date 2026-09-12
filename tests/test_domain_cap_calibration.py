"""domain 內容上限校準的抽樣、提示詞與輸出隔離。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.classify_lite import calibrate_domain_cap as calibrate
from src.classify_lite import judge_domain as judge
from src.classify_lite import sample_domain_cap_calibration as sampler


def _fake_source() -> pd.DataFrame:
    rows = []
    lengths = {"Q1": 100, "Q2": 500, "Q3": 2000, "Q4": 5000}
    index = 0
    # 兩個截斷狀態各有多個非空層，且總數固定成真實樣本的 319/281。
    for truncated, total, bands in (
        (False, 319, ("Q1", "Q2")), (True, 281, ("Q3", "Q4"))):
        for offset in range(total):
            band = bands[offset % len(bands)]
            rows.append({
                "sample_order": str(index + 1),
                "prompt_text_sha256": f"{index:064x}",
                "source_path": f"d/{index}.json",
                "unit_type": ("學院", "教職員", "服務憑證")[offset % 3],
                "length_stratum": band,
                "prompt_text_len": str(lengths[band]),
                "n_requests": "1",
                "truncated_at_1200": str(truncated),
            })
            index += 1
    return pd.DataFrame(rows)


def test校準抽樣兩臂各25且可重現():
    first, allocation = sampler.draw(_fake_source(), seed=17)
    second, _ = sampler.draw(_fake_source(), seed=17)
    assert first["prompt_text_sha256"].tolist() == second["prompt_text_sha256"].tolist()
    assert first["arm"].value_counts().to_dict() == {
        "not_truncated_at_1200": 25, "truncated_at_1200": 25}
    assert len(first) == first["prompt_text_sha256"].nunique() == 50
    assert (allocation["sample"] >= 1).all()


def test校準輸出不含明文且正式輸出路徑完全分開(tmp_path):
    sample, _ = sampler.draw(_fake_source(), seed=18)
    out = tmp_path / "sample.csv"
    sampler.write_sample(sample, out)
    saved = pd.read_csv(out, encoding="utf-8-sig")
    assert tuple(saved.columns) == sampler.OUTPUT_COLUMNS
    assert not {"prompt_text", "content", "reason"} & set(saved.columns)
    assert judge.OUTPUT_CSV not in set(calibrate.OUTPUTS.values())
    assert all("calibration" in path.name for path in calibrate.OUTPUTS.values())


def test_1200提示詞逐位元組不變而4000明列新上限():
    rows = judge.load_domain_rows()
    domain_block = judge.render_domain(rows)
    content = "測試" * 700
    original = judge.PROMPT_TEMPLATE.format(
        domain_block=domain_block,
        original_length=len(content),
        sent_length=1200,
        truncated="是",
        content=content[:1200],
    )
    assert judge.build_prompt(content, len(content), domain_block, 1200) == original
    longer = judge.build_prompt(content, len(content), domain_block, 4000)
    assert "4,000 字元上限" in longer
    assert content in longer
    schema = judge.build_json_schema()
    assert judge.prompt_fingerprint(domain_block, schema, 1200) != \
        judge.prompt_fingerprint(domain_block, schema, 4000)


def test每筆上限先後順序固定且兩種都會先出現():
    orders = {calibrate.cap_order(f"{i:064x}") for i in range(30)}
    assert orders == {(1200, 4000), (4000, 1200)}
