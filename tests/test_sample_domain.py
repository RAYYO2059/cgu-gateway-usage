"""Domain 逐筆抽樣的配額、可重現性與明文邊界。"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.classify_lite import sample_domain as sample


def _population() -> pd.DataFrame:
    rows = []
    n = 0
    for unit_type in ("學院", "教職員", "服務憑證"):
        for band, length in (("Q1", 100), ("Q2", 500), ("Q3", 2000), ("Q4", 6000)):
            for _ in range(60):
                n += 1
                rows.append({
                    "prompt_text_sha256": f"{n:064x}",
                    "source_path": f"day/{n}.json",
                    "unit_type": unit_type,
                    "prompt_text_len": length,
                    "n_requests": 1,
                    "length_stratum": band,
                    "stratum": f"{unit_type}|{band}",
                    "truncated_at_1200": length > 1200,
                })
    return pd.DataFrame(rows)


def test_長度切點的邊界():
    assert sample.length_stratum(251) == "Q1"
    assert sample.length_stratum(252) == "Q2"
    assert sample.length_stratum(986) == "Q2"
    assert sample.length_stratum(987) == "Q3"
    assert sample.length_stratum(4799) == "Q3"
    assert sample.length_stratum(4800) == "Q4"


def test_先保底再分配且總數正確():
    populations = {"大層": 1000, "中層": 100, "小層": 17}
    allocation, base = sample.allocate_sample(populations, total=90, minimum=15)
    assert base == {"大層": 15, "中層": 15, "小層": 15}
    assert sum(allocation.values()) == 90
    assert all(allocation[key] >= 15 for key in populations)
    assert all(allocation[key] <= populations[key] for key in populations)


def test_母體不足保底時全取且不被回收():
    populations = {"大層": 100, "不足層": 7, "剛好層": 15}
    allocation, base = sample.allocate_sample(populations, total=60, minimum=15)
    assert base["不足層"] == 7
    assert allocation["不足層"] == 7
    assert allocation["剛好層"] == 15
    assert sum(allocation.values()) == 60


def test_抽樣固定種子可重現(monkeypatch):
    population = _population()
    monkeypatch.setattr(sample, "SAMPLE_SIZE", 120)
    monkeypatch.setattr(sample, "MIN_PER_STRATUM", 5)
    one, alloc_one = sample.draw_sample(population, seed=123)
    two, alloc_two = sample.draw_sample(population, seed=123)
    assert one["prompt_text_sha256"].tolist() == two["prompt_text_sha256"].tolist()
    pd.testing.assert_frame_equal(alloc_one, alloc_two)


def test_樣本輸出不含明文欄(tmp_path, monkeypatch):
    population = _population()
    monkeypatch.setattr(sample, "SAMPLE_SIZE", 120)
    monkeypatch.setattr(sample, "MIN_PER_STRATUM", 5)
    drawn, _ = sample.draw_sample(population, seed=123)
    out = tmp_path / "domain_sample.csv"
    sample.write_sample(drawn, out)
    with out.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        assert tuple(reader.fieldnames) == sample.OUTPUT_COLUMNS
    assert len(rows) == 120
    assert "prompt_text" not in rows[0]
    assert len({row["prompt_text_sha256"] for row in rows}) == 120
