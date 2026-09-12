"""從 600 筆 domain 樣本抽 50 筆做 1,200／4,000 字元上限校準。

兩個截斷狀態各抽 25 筆；狀態內再依 unit_type × 長度帶分層，每個非空層
先保底 1 筆，再依該狀態在 600 樣本中的層大小比例分配剩餘。輸出不含明文。
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

import pandas as pd

from src.classify_lite.sample_domain import allocate_sample


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
INPUT_CSV = SCRATCHPAD / "domain_sample.csv"
OUTPUT_CSV = SCRATCHPAD / "domain_cap_calibration_sample.csv"

SEED = 2026091203
SAMPLE_PER_ARM = 25
EXPECTED_INPUT = 600
EXPECTED_TRUNCATED = 281
OUTPUT_COLUMNS = (
    "calibration_order", "sample_order", "prompt_text_sha256", "source_path",
    "unit_type", "length_stratum", "prompt_text_len", "n_requests",
    "truncated_at_1200", "arm", "arm_stratum_population",
    "arm_stratum_sample_size",
)


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def draw(source: pd.DataFrame, seed: int = SEED) -> tuple[pd.DataFrame, pd.DataFrame]:
    if len(source) != EXPECTED_INPUT:
        raise ValueError(f"domain 樣本應為 {EXPECTED_INPUT} 筆，實際 {len(source)}")
    if source["prompt_text_sha256"].duplicated().any():
        raise ValueError("domain 樣本有重複 sha256")
    work = source.copy()
    work["is_truncated"] = work["truncated_at_1200"].map(_as_bool)
    if int(work["is_truncated"].sum()) != EXPECTED_TRUNCATED:
        raise ValueError("1,200 字元截斷數不是凍結的 281 筆")
    work["arm"] = work["is_truncated"].map(
        {True: "truncated_at_1200", False: "not_truncated_at_1200"})
    work["arm_stratum"] = (
        work["unit_type"].astype(str) + "|" + work["length_stratum"].astype(str))

    rng = random.Random(seed)
    picked: list[int] = []
    allocation_rows: list[dict[str, object]] = []
    for arm in ("not_truncated_at_1200", "truncated_at_1200"):
        arm_rows = work[work["arm"] == arm]
        populations = arm_rows["arm_stratum"].value_counts().sort_index().to_dict()
        allocation, base = allocate_sample(
            populations, total=SAMPLE_PER_ARM, minimum=1)
        for stratum in sorted(populations):
            candidates = sorted(
                arm_rows.index[arm_rows["arm_stratum"] == stratum].tolist())
            picked.extend(rng.sample(candidates, allocation[stratum]))
            unit_type, length_stratum = stratum.rsplit("|", 1)
            allocation_rows.append({
                "arm": arm,
                "unit_type": unit_type,
                "length_stratum": length_stratum,
                "population_in_600": populations[stratum],
                "base": base[stratum],
                "proportional_extra": allocation[stratum] - base[stratum],
                "sample": allocation[stratum],
            })
    rng.shuffle(picked)
    sample = work.loc[picked].copy().reset_index(drop=True)
    sample.insert(0, "calibration_order", range(1, len(sample) + 1))
    populations = work.groupby(["arm", "arm_stratum"]).size()
    allocations = sample.groupby(["arm", "arm_stratum"]).size()
    sample["arm_stratum_population"] = sample.apply(
        lambda row: int(populations[(row["arm"], row["arm_stratum"])]), axis=1)
    sample["arm_stratum_sample_size"] = sample.apply(
        lambda row: int(allocations[(row["arm"], row["arm_stratum"])]), axis=1)
    if len(sample) != 2 * SAMPLE_PER_ARM:
        raise AssertionError("校準樣本不是 50 筆")
    if sample["prompt_text_sha256"].duplicated().any():
        raise AssertionError("校準樣本有重複 sha256")
    if sample["arm"].value_counts().to_dict() != {
        "not_truncated_at_1200": 25, "truncated_at_1200": 25,
    }:
        raise AssertionError("兩個截斷狀態不是各 25 筆")
    return sample, pd.DataFrame(allocation_rows)


def write_sample(sample: pd.DataFrame, path: Path = OUTPUT_CSV) -> None:
    forbidden = {"prompt_text", "prompt", "content", "message", "reason"}
    if forbidden & set(OUTPUT_COLUMNS):
        raise AssertionError("校準樣本欄位含明文")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_COLUMNS))
        writer.writeheader()
        writer.writerows(sample.loc[:, OUTPUT_COLUMNS].to_dict("records"))


def main() -> int:
    source = pd.read_csv(INPUT_CSV, encoding="utf-8-sig", dtype=str,
                         keep_default_na=False)
    sample, allocation = draw(source)
    write_sample(sample)
    print(f"固定校準抽樣種子 {SEED}")
    print("截斷／未截斷各 25 筆；狀態內 unit_type × 長度帶先保底 1 再按比例")
    print(allocation.to_string(index=False))
    print(f"校準樣本 {len(sample)} 筆；明文欄位 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
