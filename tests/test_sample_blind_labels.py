"""人工盲標抽樣的安全性與可重現性測試。"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.classify_lite import sample_blind_labels as sample


def _fixtures():
    dispositions = {
        "batch_project": 30,
        "user_envelope": 20,
        "tool_injected": 13,
        "service_relay": 11,
        "insufficient": 1,
    }
    rows, screen = [], []
    n = 1
    for disposition, count in dispositions.items():
        for _ in range(count):
            gid = f"G{n:03d}"
            rows.append({
                "group_id": gid,
                "disposition": disposition,
                "prompt_sha256": sample.FINGERPRINT,
            })
            screen.append({"group_id": gid, "personal_data": "clean"})
            n += 1
    # build_sample 對真實母體有硬斷言；補到 225 群但都用 batch_project。
    while len(rows) < 225:
        gid = f"G{n:03d}"
        rows.append({
            "group_id": gid,
            "disposition": "batch_project",
            "prompt_sha256": sample.FINGERPRINT,
        })
        screen.append({"group_id": gid, "personal_data": "clean"})
        n += 1
    return pd.DataFrame(screen), pd.DataFrame(rows)


def test_固定種子可重現(monkeypatch):
    screen, judged = _fixtures()
    monkeypatch.setattr(sample, "EXCLUDED_GROUPS", frozenset())
    one = sample.build_sample(screen, judged)[0]
    two = sample.build_sample(screen, judged)[0]
    assert one == two


def test_稀有類全取且常見類照配額(monkeypatch):
    screen, judged = _fixtures()
    monkeypatch.setattr(sample, "EXCLUDED_GROUPS", frozenset())
    rows, selected, eligible = sample.build_sample(screen, judged)
    assert selected == {
        "batch_project": 20,
        "user_envelope": 15,
        "tool_injected": 13,
        "service_relay": 11,
        "insufficient": 1,
    }
    assert len(rows) == 60
    for disposition in ("tool_injected", "service_relay", "insufficient"):
        assert selected[disposition] == eligible[disposition]


def test_排除群與非_clean_都不會入樣本(monkeypatch):
    screen, judged = _fixtures()
    excluded = judged.loc[judged["disposition"] == "tool_injected", "group_id"].iloc[0]
    monkeypatch.setattr(sample, "EXCLUDED_GROUPS", frozenset({excluded}))
    rows = sample.build_sample(screen, judged)[0]
    assert excluded not in {row["group_id"] for row in rows}

    flagged = screen.iloc[0]["group_id"]
    screen.loc[screen["group_id"] == flagged, "personal_data"] = "flagged"
    with pytest.raises(ValueError, match="clean 群應為 225"):
        sample.build_sample(screen, judged)


def test_輸出只有_group_id_與_opaque_分層(tmp_path, monkeypatch):
    screen, judged = _fixtures()
    monkeypatch.setattr(sample, "EXCLUDED_GROUPS", frozenset())
    rows = sample.build_sample(screen, judged)[0]
    out = tmp_path / "sample.csv"
    sample.write_sample(rows, out)
    with out.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        saved = list(reader)
        assert tuple(reader.fieldnames) == sample.OUTPUT_COLUMNS
    assert saved
    assert all(row["stratum"].startswith("S") for row in saved)
    forbidden = {"disposition", "frame_owner", "axis_level", "reason",
                 "confidence", "prompt_text", "prefix"}
    assert not forbidden & set(saved[0])


def test_錯指紋直接停止(monkeypatch):
    screen, judged = _fixtures()
    monkeypatch.setattr(sample, "EXCLUDED_GROUPS", frozenset())
    judged.loc[0, "prompt_sha256"] = "wrong"
    with pytest.raises(ValueError, match="指紋不符"):
        sample.build_sample(screen, judged)
