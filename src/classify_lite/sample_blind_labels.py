"""抽取分流結果的人工盲標樣本。

只讀 ``prefix_screen.csv`` 的篩檢結果與現行指紋判定檔的
``group_id``／``disposition``／``prompt_sha256``。輸出只有 ``group_id``
與不直接揭露模型判定的 opaque ``stratum``；不讀也不輸出前綴、
``prompt_text``、``reason`` 或其他判定欄位。

執行：``python -m src.classify_lite.sample_blind_labels``
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
SCREEN_CSV = SCRATCHPAD / "prefix_screen.csv"
JUDGE_CSV = SCRATCHPAD / "judge_output.e13f9738.csv"
OUTPUT_CSV = SCRATCHPAD / "blind_label_sample.csv"

FINGERPRINT = "e13f9738fba32a8f5167807157718758954dc2e2f1304392bdf86b3b7dd97114"
SEED = 20260911
EXCLUDED_GROUPS = frozenset({
    "A008", "A010", "A013", "A017", "A018", "A019", "A020",
})

# None 表示排除已看過的群之後全取。stratum 使用 opaque code，避免盲標檔
# 本身把個別群的模型判定交給標註者。分層對應留在可重現的抽樣程式裡。
STRATA = {
    "batch_project": (20, "S01"),
    "user_envelope": (15, "S02"),
    "tool_injected": (None, "S03"),
    "service_relay": (None, "S04"),
    "insufficient": (None, "S05"),
}
OUTPUT_COLUMNS = ("group_id", "stratum")


def build_sample(screen: pd.DataFrame, judged: pd.DataFrame):
    """回傳 ``(sample_rows, selected_counts, eligible_counts)``。"""
    if screen["group_id"].duplicated().any():
        raise ValueError("prefix_screen.csv 的 group_id 重複")
    if judged["group_id"].duplicated().any():
        raise ValueError("judge_output 的 group_id 重複")

    clean = set(screen.loc[
        screen["personal_data"].astype(str).str.strip() == "clean", "group_id"
    ].astype(str))
    if len(clean) != 225:
        raise ValueError(f"clean 群應為 225，實際 {len(clean)}")

    fingerprints = set(judged["prompt_sha256"].dropna().astype(str))
    if fingerprints != {FINGERPRINT}:
        raise ValueError(f"判定檔指紋不符：{len(fingerprints)} 種")
    judged_ids = set(judged["group_id"].astype(str))
    if judged_ids != clean:
        raise ValueError(
            "現行判定檔必須恰好覆蓋 225 個 clean 群；"
            f"缺 {len(clean - judged_ids)}、多 {len(judged_ids - clean)}")

    pool = judged[
        judged["group_id"].isin(clean - EXCLUDED_GROUPS)
    ].copy()
    rng = random.Random(SEED)
    rows = []
    selected_counts = {}
    eligible_counts = {}
    for disposition, (target, code) in STRATA.items():
        ids = sorted(pool.loc[
            pool["disposition"] == disposition, "group_id"
        ].astype(str))
        eligible_counts[disposition] = len(ids)
        if target is None:
            selected = ids
        else:
            if len(ids) < target:
                raise ValueError(
                    f"{disposition} 排除後只有 {len(ids)} 群，少於目標 {target}")
            selected = rng.sample(ids, target)
        selected_counts[disposition] = len(selected)
        rows.extend({"group_id": gid, "stratum": code} for gid in selected)

    rng.shuffle(rows)
    selected_ids = {row["group_id"] for row in rows}
    if len(selected_ids) != len(rows):
        raise AssertionError("抽樣結果有重複 group_id")
    if selected_ids & EXCLUDED_GROUPS:
        raise AssertionError("抽樣結果包含已看過的群")
    if not selected_ids <= clean:
        raise AssertionError("抽樣結果包含非 clean 群")
    return rows, selected_counts, eligible_counts


def write_sample(rows, path: Path = OUTPUT_CSV) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(OUTPUT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    screen = pd.read_csv(
        SCREEN_CSV, encoding="utf-8-sig", dtype=str,
        usecols=["group_id", "personal_data"])
    judged = pd.read_csv(
        JUDGE_CSV, encoding="utf-8-sig", dtype=str,
        usecols=["group_id", "disposition", "prompt_sha256"])
    rows, selected, eligible = build_sample(screen, judged)
    write_sample(rows)

    print(f"固定種子 {SEED}")
    print("母體：225 個 prefix_screen=clean 且有現行指紋判定的群")
    print(f"排除已看過 {len(EXCLUDED_GROUPS)} 群")
    for disposition in STRATA:
        print(f"{disposition}: 排除後 {eligible[disposition]}，抽取 {selected[disposition]}")
    print(f"合計抽取 {len(rows)} 群")
    print(f"輸出 {OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
