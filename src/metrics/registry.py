"""指標註冊機制。

每個指標是一個帶元資料的函數。註冊之後可以按名稱查詢、執行，
文件也由 registry 自動產生——手寫的文件會過期，產生的不會。

抑制規則刻意放在 registry 層而不是各指標內部：
放在指標裡的話，新增指標的人必須記得自己套規則，忘記就是靜默外洩。
放在這裡，只要宣告了 group_by 就自動生效。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src import config

logger = logging.getLogger(__name__)

UNITS = ("request", "turn", "thread", "user")
SOURCES = ("request", "turn", "thread", "user")

# 比例欄位的命名慣例。抑制只清掉比例，不清掉計數，所以必須認得出哪些是比例。
# 指標作者若用了不符慣例的欄名，可在 MetricResult 明確指定 ratio_columns。
RATIO_SUFFIXES = ("_share", "_ratio", "_pct", "_rate", "_佔比", "佔比", "_比例", "比例")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class MetricResult:
    """單一指標的執行結果。"""

    data: pd.DataFrame
    n_total: int
    n_covered: int
    coverage: float = field(init=False)
    suppressed: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # 觸發抑制時要清成 NA 的欄位。None 表示依 RATIO_SUFFIXES 慣例推斷，
    # 空 list 表示「這個指標沒有需要抑制的欄位」（明確宣告，不發警告）。
    #
    # 名稱沿用 ratio_columns，但語意是「受抑制欄位」：比例是最常見的一種，
    # 分位數之類描述「這一組的行為」的統計量同樣該抑制——
    # 當一組被單人主導時，它的中位數就是在描述那個人。
    ratio_columns: list | None = None
    # 這個指標的輸出裡含有未被抑制的彙總列（「全體」之類）。
    #
    # 宣告它不會改變抑制行為，只會在 suppression_reason 裡加註一句實話：
    # 有彙總列時，被抑制那組的數值可以從「彙總 vs 其他組」的對照推回來，
    # 抑制擋不住。假裝擋住了比不抑制更糟——讀者會誤以為那個數字取不到。
    has_unsuppressed_total_row: bool = False

    def __post_init__(self) -> None:
        self.coverage = (self.n_covered / self.n_total) if self.n_total else 0.0


@dataclass(frozen=True)
class MetricSpec:
    name: str
    question: str
    unit: str
    source: str
    denominator: str
    caveat: str | None
    needs_dedup: bool
    group_by: list | None
    version: str
    fn: Callable[[dict], MetricResult]


REGISTRY: dict[str, MetricSpec] = {}


def metric(
    *,
    name: str,
    question: str,
    unit: str,
    source: str,
    denominator: str,
    caveat: str | None = None,
    needs_dedup: bool = False,
    group_by: list | None = None,
    version: str = "1.0",
):
    """把函數註冊成指標。函數簽名須為 fn(tables: dict) -> MetricResult。"""

    def decorator(fn: Callable[[dict], MetricResult]) -> Callable[[dict], MetricResult]:
        if name in REGISTRY:
            # 靜默覆蓋會讓兩個指標共用一個名字，其中一個永遠不會被執行，
            # 而 INDEX.md 只會列出一個——沒有人會發現少了東西。
            existing = REGISTRY[name]
            raise ValueError(
                f"指標名稱重複：{name!r} 已由 "
                f"{existing.fn.__module__}.{existing.fn.__qualname__} 註冊，"
                f"不可被 {fn.__module__}.{fn.__qualname__} 覆蓋"
            )
        if not _NAME_RE.match(name):
            raise ValueError(f"指標名稱須為 snake_case：{name!r}")
        if unit not in UNITS:
            raise ValueError(f"unit 須為 {UNITS} 之一，收到 {unit!r}")
        if source not in SOURCES:
            raise ValueError(f"source 須為 {SOURCES} 之一，收到 {source!r}")

        REGISTRY[name] = MetricSpec(
            name=name, question=question, unit=unit, source=source,
            denominator=denominator, caveat=caveat, needs_dedup=needs_dedup,
            group_by=list(group_by) if group_by else None,
            version=version, fn=fn,
        )
        return fn

    return decorator


# ---------------------------------------------------------------------------
# 抑制
# ---------------------------------------------------------------------------
def find_concentration(run_id: str) -> Path | None:
    """本次 run 的 concentration.csv；沒有就退回最近一次的。

    metrics 可以獨立於 aggregate 執行（run_id 不同），此時本次 run 目錄
    底下不會有 concentration.csv。退回最近一次並記 log，比直接放棄抑制安全。
    """
    current = config.RUNS_DIR / run_id / "concentration.csv"
    if current.exists():
        return current
    candidates = sorted(config.RUNS_DIR.glob("*/concentration.csv"))
    if not candidates:
        return None
    fallback = candidates[-1]
    logger.warning(
        "本次 run 沒有 concentration.csv，改用 %s。"
        "若聚合結果已過時，抑制判定可能不準——建議先跑 aggregate。",
        fallback.parent.name,
    )
    return fallback


_EMPTY_RULES = ["維度", "分組值", "below_min_group_size", "dominant"]


def _read_concentration(path: Path) -> pd.DataFrame:
    rules = pd.read_csv(path, encoding="utf-8-sig", dtype={"分組值": str})
    for column in ("below_min_group_size", "dominant"):
        rules[column] = rules[column].astype(str).str.lower().isin(["true", "1"])
    return rules


def load_suppression_rules(run_id: str) -> pd.DataFrame:
    """回傳 (維度, 分組值) → 是否需抑制。"""
    path = find_concentration(run_id)
    if path is None:
        logger.warning("找不到 concentration.csv，本次不套用抑制規則")
        return pd.DataFrame(columns=_EMPTY_RULES)
    return _read_concentration(path)


def _ratio_columns(result: MetricResult) -> list:
    if result.ratio_columns is not None:
        return [c for c in result.ratio_columns if c in result.data.columns]
    return [
        column for column in result.data.columns
        if any(str(column).endswith(suffix) for suffix in RATIO_SUFFIXES)
    ]


# 抑制理由要跟著數字走。只寫進 sidecar json 的話，直接讀 csv 的人看到 NA
# 完全不知道那是計算失敗還是政策抑制——而讀 csv 的人是多數。
REASON_COLUMN = "suppression_reason"

# 豁免維度也要填字，不留空欄。整欄空白會被當成「這欄壞了」，
# 寫明「為什麼這裡不抑制」才是有用的資訊。
_EXEMPT_REASON = "依政策豁免抑制（此維度分的是請求不是人）"

_TOTAL_ROW_NOTE = "同表彙總列未抑制，本列數值可由對照推得"


def apply_suppression(
    spec: MetricSpec, result: MetricResult, rules: pd.DataFrame
) -> MetricResult:
    """對宣告了 group_by 的指標套用抑制，並把理由寫進主表。

    計數欄位（n_users、n_requests…）一律保留：計數是事實。
    比例欄位置為 NA：比例才有再識別與誤導風險。
    「16 個系所有人使用」可以說，「某系所佔 45%」不行。

    每一列另外附上 suppression_reason：被抑制的列寫原因，
    豁免維度寫豁免理由，其餘留空。
    """
    if not spec.group_by or result.data.empty:
        return result

    # 先建欄位再談規則：即使 concentration.csv 缺席、即使這個指標沒有可抑制的
    # 欄位，宣告了 group_by 的輸出都必須帶著這一欄，讀者才知道有沒有被動過手腳。
    result.data[REASON_COLUMN] = ""
    reasons_by_row: dict[int, list[str]] = {}

    ratio_columns = _ratio_columns(result)
    if not ratio_columns and result.ratio_columns is None:
        # 只有「沒明講、也推不出來」時才警告。明確宣告空 list 是有意的。
        result.warnings.append(
            f"指標宣告了 group_by={spec.group_by} 但找不到受抑制欄位"
            f"（慣例後綴 {RATIO_SUFFIXES}），本次沒有任何欄位被抑制"
        )

    from src import aggregate

    for dimension in spec.group_by:
        if dimension not in result.data.columns:
            result.warnings.append(f"group_by 宣告的維度 {dimension!r} 不在輸出欄位裡")
            continue

        # 政策性豁免：不是「把人分群」的維度不抑制（見 aggregate 的說明）。
        # 代價是讀者看不到母數厚薄，所以改用強制附 n_users 來補——
        # 少了它，「凌晨 3 點 100% 是某某模型」這種一人一格的數字會裸奔出去。
        if dimension not in aggregate.CONCENTRATION_DIMENSIONS:
            if "n_users" not in result.data.columns:
                result.warnings.append(
                    f"維度 {dimension!r} 依政策不套用抑制（它分的是請求不是人），"
                    "但輸出缺少 n_users 欄位——讀者將無從判斷母數，請補上"
                )
            for index in result.data.index:
                reasons_by_row.setdefault(index, []).append(_EXEMPT_REASON)
            continue

        if rules.empty:
            result.warnings.append(
                f"維度 {dimension!r} 在抑制範圍內，但找不到 concentration.csv"
                "：本次未套用抑制"
            )
            continue

        applicable = rules[rules["維度"] == dimension]
        if applicable.empty:
            result.warnings.append(
                f"維度 {dimension!r} 在抑制範圍內，但 concentration.csv 沒有它的紀錄"
                "：聚合結果可能過時，請重跑 aggregate"
            )
            continue

        if not ratio_columns:
            continue

        flagged = applicable[applicable["below_min_group_size"]
                             | applicable["dominant"]]
        for row in flagged.itertuples():
            mask = result.data[dimension].astype(str) == str(row.分組值)
            if not mask.any():
                continue
            result.data.loc[mask, ratio_columns] = pd.NA
            reasons = []
            if row.below_min_group_size:
                reasons.append(f"母數 {row.n_users} < {config.MIN_GROUP_SIZE}")
            if row.dominant:
                reasons.append(f"單人佔 {100 * float(row.top1_user_share):.1f}%"
                               f" > {100 * config.DOMINANT_THRESHOLD:.0f}%")
            reason = "、".join(reasons)
            for index in result.data.index[mask]:
                reasons_by_row.setdefault(index, []).append(f"{dimension}：{reason}")
            result.suppressed.append({
                "維度": dimension,
                "分組值": str(row.分組值),
                "原因": reason,
                "被抑制欄位": ",".join(map(str, ratio_columns)),
            })

    # 有彙總列時據實說明抑制擋不住，只加在真的被抑制的列上——
    # 豁免列沒有東西被擋，不需要這句。
    suppressed_index = {
        index for index, items in reasons_by_row.items()
        if any(item != _EXEMPT_REASON for item in items)
    }
    if result.has_unsuppressed_total_row:
        for index in suppressed_index:
            reasons_by_row[index].append(_TOTAL_ROW_NOTE)

    for index, items in reasons_by_row.items():
        result.data.loc[index, REASON_COLUMN] = "；".join(items)
    return result


# ---------------------------------------------------------------------------
# 執行
# ---------------------------------------------------------------------------
def load_tables() -> dict:
    """組出 {"request", "turn", "thread", "user"} 四張表。"""
    from src import aggregate, schema

    missing = [p for p in (aggregate.TURN_PATH, aggregate.THREAD_PATH,
                           aggregate.USER_PATH) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"缺少聚合表 {[str(p) for p in missing]}，請先執行 python -m src.run aggregate"
        )
    return {
        "request": schema.load_dataset(),
        "turn": pd.read_parquet(aggregate.TURN_PATH),
        "thread": pd.read_parquet(aggregate.THREAD_PATH),
        "user": pd.read_parquet(aggregate.USER_PATH),
    }


def run_metric(spec: MetricSpec, tables: dict, rules: pd.DataFrame) -> MetricResult:
    result = spec.fn(tables)
    if not isinstance(result, MetricResult):
        raise TypeError(
            f"指標 {spec.name} 必須回傳 MetricResult，收到 {type(result).__name__}"
        )
    return apply_suppression(spec, result, rules)


def list_metrics() -> list[MetricSpec]:
    return [REGISTRY[name] for name in sorted(REGISTRY)]
