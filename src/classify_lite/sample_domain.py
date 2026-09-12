"""從需送分類器的相異內容母體抽出 domain 逐筆判定樣本。

抽樣單位是 ``prompt_text_sha256``，不是請求。程式只寫雜湊與中繼資料，
不讀、不中繼、也不輸出 ``prompt_text`` 明文。

執行：``python -m src.classify_lite.sample_domain``
"""

from __future__ import annotations

import csv
import math
import random
from pathlib import Path

import pandas as pd

from src import aggregate_lite, extract_lite
from src.classify_lite import prefilter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
OUTPUT_CSV = SCRATCHPAD / "domain_sample.csv"

SAMPLE_SIZE = 600
MIN_PER_STRATUM = 15
SEED = 2026091102
LENGTH_CUTS = (251, 986, 4799)
EXPECTED_ELIGIBLE_SHA = 17_365
EXPECTED_ELIGIBLE_REQUESTS = 58_100

OUTPUT_COLUMNS = (
    "sample_order", "prompt_text_sha256", "source_path", "unit_type",
    "length_stratum", "prompt_text_len", "n_requests", "truncated_at_1200",
    "stratum_population", "stratum_sample_size", "sample_weight",
)


def length_stratum(length: int) -> str:
    """依凍結切點把字元長度分到 Q1–Q4；切點本身歸較短一側。"""
    if length <= LENGTH_CUTS[0]:
        return "Q1"
    if length <= LENGTH_CUTS[1]:
        return "Q2"
    if length <= LENGTH_CUTS[2]:
        return "Q3"
    return "Q4"


def _largest_remainder(weights: dict[str, int], total: int) -> dict[str, int]:
    """Hamilton 最大餘數法；同餘數時以母體數、層名作穩定排序。"""
    if total < 0:
        raise ValueError("待分配數不可為負")
    if not weights:
        if total:
            raise ValueError("沒有可分配的層")
        return {}
    denom = sum(weights.values())
    if denom <= 0:
        raise ValueError("分配權重總和必須大於零")
    quota = {key: total * value / denom for key, value in weights.items()}
    result = {key: math.floor(value) for key, value in quota.items()}
    left = total - sum(result.values())
    order = sorted(
        weights,
        key=lambda key: (quota[key] - result[key], weights[key], key),
        reverse=True,
    )
    for key in order[:left]:
        result[key] += 1
    return result


def allocate_sample(populations: dict[str, int], total: int = SAMPLE_SIZE,
                    minimum: int = MIN_PER_STRATUM) -> tuple[dict[str, int], dict[str, int]]:
    """先給保底，再依原母體占比分配剩餘；小層全取並退出後續分配。

    回傳 ``(allocation, base)``。若比例配額撞到層容量，先封頂，再把餘額
    重新分配給尚有容量的層；因此小於保底的層永遠是全取，不會在回收階段
    把保底扣掉。
    """
    if total > sum(populations.values()):
        raise ValueError("樣本數大於母體")
    if minimum < 0:
        raise ValueError("保底不可為負")

    base = {key: min(minimum, size) for key, size in populations.items()}
    allocation = dict(base)
    remaining = total - sum(allocation.values())
    if remaining < 0:
        raise ValueError("樣本數不足以容納各層保底")

    while remaining:
        open_strata = {
            key: populations[key]
            for key in populations
            if allocation[key] < populations[key]
        }
        if not open_strata:
            raise AssertionError("仍有配額但所有層都已全取")
        proposal = _largest_remainder(open_strata, remaining)
        used = 0
        for key, want in proposal.items():
            take = min(want, populations[key] - allocation[key])
            allocation[key] += take
            used += take
        if used == 0:
            # 最大餘數法在 remaining < 層數時仍會配置至少一筆；到這裡代表
            # 演算法或容量檢查壞了，不能用補滿掩蓋。
            raise AssertionError("比例分配沒有前進")
        remaining -= used

    if sum(allocation.values()) != total:
        raise AssertionError("分配總數不等於樣本數")
    for key, size in populations.items():
        if not 0 <= allocation[key] <= size:
            raise AssertionError(f"{key} 的配額超過母體")
        if size < minimum and allocation[key] != size:
            raise AssertionError(f"{key} 小於保底卻沒有全取")
        if size >= minimum and allocation[key] < minimum:
            raise AssertionError(f"{key} 的保底被回收")
    return allocation, base


def build_population(frame: pd.DataFrame, users: pd.DataFrame,
                     assignments: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """建立每個 eligible sha256 一列的抽樣母體，回傳跨 unit_type 的 sha 數。"""
    required_frame = {
        "prompt_text_sha256", "prompt_text_len", "source_path", "request_id",
        "anonymous_user_id", "ts_utc",
    }
    missing = required_frame - set(frame.columns)
    if missing:
        raise ValueError(f"請求表缺欄位：{sorted(missing)}")
    if not {"anonymous_user_id", "unit_type"} <= set(users.columns):
        raise ValueError("user_lite 缺 anonymous_user_id 或 unit_type")
    if not {"prompt_text_sha256", "rule", "n_requests"} <= set(assignments.columns):
        raise ValueError("前置規則表欄位不完整")
    if assignments["prompt_text_sha256"].duplicated().any():
        raise ValueError("前置規則表不是每個 sha256 一列")

    eligible = assignments.loc[
        assignments["rule"] == prefilter.UNASSIGNED,
        ["prompt_text_sha256", "n_requests"],
    ].copy()
    if len(eligible) != EXPECTED_ELIGIBLE_SHA:
        raise ValueError(
            f"需送分類器的相異內容應為 {EXPECTED_ELIGIBLE_SHA}，實際 {len(eligible)}")
    if int(eligible["n_requests"].sum()) != EXPECTED_ELIGIBLE_REQUESTS:
        raise ValueError(
            "需送分類器的請求數應為 "
            f"{EXPECTED_ELIGIBLE_REQUESTS}，實際 {int(eligible['n_requests'].sum())}")

    joined = frame.merge(
        users[["anonymous_user_id", "unit_type"]],
        on="anonymous_user_id", how="left", validate="many_to_one",
    )
    joined = joined.merge(
        eligible[["prompt_text_sha256"]],
        on="prompt_text_sha256", how="inner", validate="many_to_one",
    )
    if len(joined) != EXPECTED_ELIGIBLE_REQUESTS:
        raise ValueError("請求表與前置規則表對接後的請求數不符")
    if joined["unit_type"].isna().any():
        raise ValueError("eligible 請求存在缺漏的 unit_type")

    diversity = joined.groupby("prompt_text_sha256")["unit_type"].nunique()
    multi_unit = int((diversity > 1).sum())

    # 一個 sha256 只能落一層。沿用舊 valset.py 的語意：依時間排序後取
    # 該相異內容的第一筆請求；request_id 是同時戳時的穩定次序。
    first = (joined.sort_values(["ts_utc", "request_id"], kind="mergesort")
             .drop_duplicates("prompt_text_sha256", keep="first"))
    population = first[[
        "prompt_text_sha256", "source_path", "unit_type", "prompt_text_len",
    ]].merge(eligible, on="prompt_text_sha256", how="left", validate="one_to_one")
    population["prompt_text_len"] = population["prompt_text_len"].astype("int64")
    population["n_requests"] = population["n_requests"].astype("int64")
    population["length_stratum"] = population["prompt_text_len"].map(length_stratum)
    population["stratum"] = (
        population["unit_type"].astype(str) + "|" + population["length_stratum"])
    population["truncated_at_1200"] = population["prompt_text_len"] > 1200

    if len(population) != EXPECTED_ELIGIBLE_SHA:
        raise AssertionError("抽樣母體不是每個 eligible sha256 一列")
    if population["prompt_text_sha256"].duplicated().any():
        raise AssertionError("抽樣母體有重複 sha256")
    return population, multi_unit


def draw_sample(population: pd.DataFrame, seed: int = SEED,
                sample_size: int | None = None,
                minimum: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """依 unit_type × 長度帶抽樣，回傳 ``(sample, allocation_table)``。"""
    sample_size = SAMPLE_SIZE if sample_size is None else sample_size
    minimum = MIN_PER_STRATUM if minimum is None else minimum
    populations = population["stratum"].value_counts().sort_index().to_dict()
    allocation, base = allocate_sample(populations, sample_size, minimum)
    rng = random.Random(seed)
    picked: list[int] = []
    for stratum in sorted(populations):
        indices = sorted(population.index[population["stratum"] == stratum].tolist())
        picked.extend(rng.sample(indices, allocation[stratum]))
    rng.shuffle(picked)

    sample = population.loc[picked].copy().reset_index(drop=True)
    sample.insert(0, "sample_order", range(1, len(sample) + 1))
    sample["stratum_population"] = sample["stratum"].map(populations).astype(int)
    sample["stratum_sample_size"] = sample["stratum"].map(allocation).astype(int)
    sample["sample_weight"] = (
        sample["stratum_population"] / sample["stratum_sample_size"])
    if len(sample) != sample_size or sample["prompt_text_sha256"].duplicated().any():
        raise AssertionError(f"抽樣不是 {sample_size} 個不重複 sha256")

    rows = []
    for stratum in sorted(populations):
        unit_type, band = stratum.rsplit("|", 1)
        rows.append({
            "unit_type": unit_type,
            "length_stratum": band,
            "population": populations[stratum],
            "base": base[stratum],
            "proportional_extra": allocation[stratum] - base[stratum],
            "sample": allocation[stratum],
            "all_taken": allocation[stratum] == populations[stratum],
        })
    return sample, pd.DataFrame(rows)


def write_sample(sample: pd.DataFrame, path: Path = OUTPUT_CSV) -> None:
    """只寫允許欄位；欄名與值都先過明文邊界檢查。"""
    forbidden = {"prompt_text", "prompt", "content", "message", "body", "payload"}
    columns = set(OUTPUT_COLUMNS)
    if forbidden & columns:
        raise AssertionError("domain 樣本輸出欄位含明文")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(OUTPUT_COLUMNS))
        writer.writeheader()
        for row in sample.loc[:, OUTPUT_COLUMNS].to_dict("records"):
            writer.writerow(row)


def main() -> int:
    frame = extract_lite.load_dataset()
    users = pd.read_parquet(aggregate_lite.USER_LITE_PATH)
    assignments, summary = prefilter.build()
    if int(summary["相異內容"].sum()) != 31_011:
        raise AssertionError("前置規則五組總和不是 31,011")
    population, multi_unit = build_population(frame, users, assignments)
    sample, allocation = draw_sample(population)
    write_sample(sample)

    print(f"固定種子 {SEED}")
    print(f"母體 {len(population):,} 個相異內容 / {int(assignments.loc[assignments['rule'] == prefilter.UNASSIGNED, 'n_requests'].sum()):,} 筆請求")
    print(f"抽樣 {len(sample):,} 個相異內容；每層保底 {MIN_PER_STRATUM}")
    print(f"跨 unit_type 的相異內容 {multi_unit} 個；依最早請求的 unit_type 歸層")
    print(allocation.to_string(index=False))
    print(f"會在 {1200} 字元截斷 {int(sample['truncated_at_1200'].sum())} 筆")
    print(f"輸出 {OUTPUT_CSV}（不含明文）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
