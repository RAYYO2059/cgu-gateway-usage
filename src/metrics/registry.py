"""指標註冊機制。

每個指標是一個帶元資料的函數。註冊之後可以按名稱查詢、執行，
文件也由 registry 自動產生——手寫的文件會過期，產生的不會。

抑制規則刻意放在 registry 層而不是各指標內部：
放在指標裡的話，新增指標的人必須記得自己套規則，忘記就是靜默外洩。
放在這裡，只要宣告了 group_by 就自動生效。


已知缺口：沒宣告 group_by 的指標完全不進抑制流程
------------------------------------------------
上面那句「只要宣告了 group_by 就自動生效」反過來說就是：**沒宣告就不生效**。
``apply_suppression()`` 第一行就是 ``if not spec.group_by: return result``，
所以那些指標連 ``suppression_reason`` 欄位都不會有。

2026-08-26 盤點 clean 的 19 個指標，有 5 個有比例欄位卻沒宣告 ``group_by``：

    anomaly_profile                 佔比                     有 n_users
    tool_types_distribution         declared_thread_share    有 n_users
    cache_hit_by_request_position   zero_cache_share         ★ 無 n_users
    model_consistency               佔比                     ★ 無 n_users
    thread_tool_message_ratio       zero_ratio_share         ★ 無 n_users

後三個既沒有抑制、也沒有 ``n_users``，讀者無從判斷那個比例背後有幾個人。
這正是豁免政策要求「豁免就必須附 n_users」想擋的情況，只是它們根本沒走到
那個檢查。

這與「未登記維度預設抑制」是**兩個獨立的缺口**：那個管的是宣告了 group_by
之後維度怎麼分派，這個管的是根本沒宣告。反轉預設值不會碰到這五個指標。

本次不處理，留紀錄。要處理的話方向有二：讓這些指標補宣告 group_by，或是把
「有比例欄位卻沒宣告 group_by」變成一則警告——後者比較像 registry 該做的事，
因為它跟抑制規則放在這裡是同一個理由：不要依賴人記得。
"""

from __future__ import annotations

import logging
import re
from typing import Sequence
from collections.abc import Callable, Collection, Mapping
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
    # 明確登記為非自然人、因而**沒有**被抑制的分組（見 NON_PERSON_GROUPS）。
    #
    # 與 suppressed 對稱但語意相反，而且必須分開記：suppressed 的契約是
    # 「這些格子真的被改成 NA 了」，render 層據此決定哪個 `—` 要配註腳。
    # 把豁免塞進同一份清單，那個契約就破了——讀者會看到一句「已抑制」
    # 配著一個完好的數字。
    #
    # 分開之後，版面上三種狀態才分得開：
    #   —（抑制）        在 suppressed 裡，配「已抑制」註腳
    #   數字＋豁免註腳   在 exempted 裡，配「依政策不抑制」註腳
    #   數字、無註腳     兩份都不在，代表根本沒觸發規則
    exempted: list = field(default_factory=list)
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
    # 這個指標屬於哪條資料流。REGISTRY 是全域 dict，兩條線的指標一旦在同一個
    # 行程裡被 import 就會混在一起——clean 跑 metrics 時會連 lite 的一起跑，
    # 然後因為缺 turn/thread 表而整批失敗。用 line 把它們分開。
    line: str = "clean"


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
    line: str = "clean",
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
            version=version, fn=fn, line=line,
        )
        return fn

    return decorator


# ---------------------------------------------------------------------------
# 抑制
# ---------------------------------------------------------------------------
def find_concentration(run_id: str, line: str = "clean") -> Path | None:
    """本次 run 的 concentration.csv；沒有就退回最近一次的。

    metrics 可以獨立於 aggregate 執行（run_id 不同），此時本次 run 目錄
    底下不會有 concentration.csv。退回最近一次並記 log，比直接放棄抑制安全。
    """
    filename = "concentration_lite.csv" if line == "lite" else "concentration.csv"
    current = config.RUNS_DIR / run_id / filename
    if current.exists():
        return current
    candidates = sorted(config.RUNS_DIR.glob(f"*/{filename}"))
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


def load_suppression_rules(run_id: str, line: str = "clean") -> pd.DataFrame:
    """回傳 (維度, 分組值) → 是否需抑制。"""
    path = find_concentration(run_id, line)
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
# 非自然人豁免的辨識字串。render 層靠它把「豁免」與「已抑制」分開，
# 所以它是對外契約的一部分，改動等同改 csv 的內容——不要順手改措辭。
_NON_PERSON_MARK = "非自然人分組"

_TOTAL_ROW_NOTE = "同表彙總列未抑制，本列數值可由對照推得"


def apply_suppression(
    spec: MetricSpec,
    result: MetricResult,
    rules: pd.DataFrame,
    dimensions: Sequence[str] | None = None,
    exempt: Sequence[str] | None = None,
    non_person: Mapping[str, Collection[str]] | None = None,
) -> MetricResult:
    """對宣告了 group_by 的指標套用抑制，並把理由寫進主表。

    計數欄位（n_users、n_requests…）一律保留：計數是事實。
    比例欄位置為 NA：比例才有再識別與誤導風險。
    「16 個系所有人使用」可以說，「某系所佔 45%」不行。

    每一列另外附上 suppression_reason：被抑制的列寫原因，
    豁免維度寫豁免理由，其餘留空。

    dimensions / exempt / non_person 預設取 aggregate 的三份模組層級清單，
    所以 clean 的呼叫端不必改；lite 那條線傳自己的進來（人層級鍵與維度都不同）。

    non_person 只豁免 dominant 那一條：明確登記的 (維度, 值) 若**只**因為
    單人集中度被擋，比例照常公布，理由改寫成豁免說明；若同時命中母數門檻
    則照舊抑制，理由只寫母數那一條——見 aggregate.NON_PERSON_GROUPS。

    **預設是保護不是放行。** 兩份清單都沒有的維度會進抑制流程並發出警告，
    而不是安靜地豁免——理由見 aggregate.EXEMPT_DIMENSIONS 的註解。
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

    suppressed_dims = tuple(
        aggregate.CONCENTRATION_DIMENSIONS if dimensions is None else dimensions)
    exempt_dims = tuple(
        aggregate.EXEMPT_DIMENSIONS if exempt is None else exempt)
    non_person_groups = (
        aggregate.NON_PERSON_GROUPS if non_person is None else non_person)

    for dimension in spec.group_by:
        if dimension not in result.data.columns:
            result.warnings.append(f"group_by 宣告的維度 {dimension!r} 不在輸出欄位裡")
            continue

        # 政策性豁免：不是「把人分群」的維度不抑制（見 aggregate 的說明）。
        # 必須**明確登記**在 EXEMPT_DIMENSIONS 裡才走這條路——沒登記的維度
        # 走下面的抑制流程並警告，因為「忘記登記」的後果不該是靜默外洩。
        #
        # 豁免的代價是讀者看不到母數厚薄，所以改用強制附 n_users 來補——
        # 少了它，「凌晨 3 點 100% 是某某模型」這種一人一格的數字會裸奔出去。
        if dimension in exempt_dims:
            if "n_users" not in result.data.columns:
                result.warnings.append(
                    f"維度 {dimension!r} 依政策不套用抑制（它分的是請求不是人），"
                    "但輸出缺少 n_users 欄位——讀者將無從判斷母數，請補上"
                )
            for index in result.data.index:
                reasons_by_row.setdefault(index, []).append(_EXEMPT_REASON)
            continue

        # 到這裡代表這個維度要抑制。它可能是明確登記的，也可能是兩份清單都
        # 沒有的——後者是新維度沒登記，補救動作與前者完全不同，所以要分開講。
        #
        # 「沒登記」要先判：不論 concentration.csv 在不在、有沒有這個維度的
        # 紀錄，該補的動作都是登記。先看規則表的話，缺 csv 時會發出
        # 「找不到 concentration.csv」——那句話對一個從未被登記的維度是誤導，
        # 因為就算把 csv 生出來，build_concentration 也不會算它。
        if dimension not in suppressed_dims:
            result.warnings.append(
                f"維度 {dimension!r} 不在 CONCENTRATION_DIMENSIONS，"
                f"也不在 EXEMPT_DIMENSIONS，已預設納入抑制但無規則可套用"
                "：請明確登記——把人分群的維度加進 CONCENTRATION_DIMENSIONS"
                "（並重跑 aggregate 讓它進 concentration.csv），"
                "分請求的維度加進 EXEMPT_DIMENSIONS"
            )
            continue

        if rules.empty:
            result.warnings.append(
                f"維度 {dimension!r} 在抑制範圍內，但找不到 concentration.csv"
                "：本次未套用抑制"
            )
            continue

        applicable = rules[rules["維度"] == dimension]
        if applicable.empty:
            # 已登記卻查無紀錄：規則表跟不上，重算就會有。
            result.warnings.append(
                f"維度 {dimension!r} 在抑制範圍內，但 concentration.csv 沒有它的紀錄"
                "：聚合結果可能過時，請重跑 aggregate"
            )
            continue

        if not ratio_columns:
            continue

        flagged = applicable[applicable["below_min_group_size"]
                             | applicable["dominant"]]
        non_person_values = non_person_groups.get(dimension, ())
        for row in flagged.itertuples():
            mask = result.data[dimension].astype(str) == str(row.分組值)
            if not mask.any():
                continue

            # 非自然人分組：只豁免 dominant，母數門檻照舊。
            #
            # 比對維度**與**值，不是只比對值——`dimension` 已經是這一圈的
            # 維度，`non_person_values` 是它自己的值集合，所以同名值出現在
            # 別的維度時查不到，會照舊走下面的抑制流程。
            #
            # 條件寫成「dominant 且非 below_min」而不是先扣掉 below_min：
            # 兩條同時命中時要的是「完全照舊」，包含理由只寫母數那一條。
            if (str(row.分組值) in non_person_values
                    and row.dominant and not row.below_min_group_size):
                share = 100 * float(row.top1_user_share)
                reason = (
                    f"{_NON_PERSON_MARK}，單一識別碼佔 {share:.1f}%"
                    f" > {100 * config.DOMINANT_THRESHOLD:.0f}%，"
                    "依政策不抑制（抑制保護的是自然人）")
                for index in result.data.index[mask]:
                    reasons_by_row.setdefault(index, []).append(
                        f"{dimension}：{reason}")
                result.exempted.append({
                    "維度": dimension,
                    "分組值": str(row.分組值),
                    "原因": reason,
                    "未抑制欄位": ",".join(map(str, ratio_columns)),
                })
                continue
            # **只記真的被改動的欄位。** 有些格子在抑制之前就是 NA——
            # 那不是「算得出來但不給看」，是「本來就沒有這個值」（例如服務憑證
            # 的 attributable_share：它不在可歸屬母體裡）。兩者印出來都是同一個
            # 破折號，讀者無從分辨，而它們的意思相反。
            #
            # 分辨的資訊只存在於這一刻——賦值之後就永遠分不出來了，
            # 所以要在覆蓋前先算。
            changed = [c for c in ratio_columns
                       if result.data.loc[mask, c].notna().any()]
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
                "被抑制欄位": ",".join(map(str, changed)),
            })

    # 有彙總列時據實說明抑制擋不住，只加在真的被抑制的列上——
    # 豁免列沒有東西被擋，不需要這句。
    # 非自然人豁免同樣不算「被抑制」：那一列的數字完好，附上「抑制擋不住」
    # 這句話會讓讀者以為它被擋過。
    suppressed_index = {
        index for index, items in reasons_by_row.items()
        if any(item != _EXEMPT_REASON and _NON_PERSON_MARK not in item
               for item in items)
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
def load_tables_lite() -> dict:
    """組出 lite 的 {"request", "user"} 兩張表。

    lite 沒有 turn/thread：那兩層要靠 turn_id / thread_id 的父子關係組出來，
    而 lite 匯出只有 thread_id、沒有 turn_id。所以 lite 的指標宣告 source
    時只能用 "request" 或 "user"。
    """
    from src import aggregate_lite, extract_lite

    if not aggregate_lite.USER_LITE_PATH.exists():
        raise FileNotFoundError(
            f"缺少 {aggregate_lite.USER_LITE_PATH}，"
            "請先執行 python -m src.aggregate_lite"
        )
    return {
        "request": extract_lite.load_dataset(),
        "user": pd.read_parquet(aggregate_lite.USER_LITE_PATH),
    }


def load_tables() -> dict:
    """組出 clean 的 {"request", "turn", "thread", "user"} 四張表。"""
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


def dimension_lists(
    line: str,
) -> tuple[tuple[str, ...], tuple[str, ...], Mapping[str, Collection[str]]]:
    """某條線的 (要抑制的維度, 明確豁免的維度, 非自然人分組)。

    寫成查表而不是讓呼叫端自己傳：漏傳的後果是整條線的維度都被判成「未登記」，
    雖然會警告，但那是每個指標各警告一次的噪音，很容易被當成雜訊略過。
    """
    from src import aggregate

    if line == "lite":
        from src import aggregate_lite

        return (aggregate_lite.CONCENTRATION_DIMENSIONS_LITE,
                aggregate_lite.EXEMPT_DIMENSIONS_LITE,
                aggregate_lite.NON_PERSON_GROUPS_LITE)
    return (aggregate.CONCENTRATION_DIMENSIONS, aggregate.EXEMPT_DIMENSIONS,
            aggregate.NON_PERSON_GROUPS)


def run_metric(spec: MetricSpec, tables: dict, rules: pd.DataFrame) -> MetricResult:
    result = spec.fn(tables)
    if not isinstance(result, MetricResult):
        raise TypeError(
            f"指標 {spec.name} 必須回傳 MetricResult，收到 {type(result).__name__}"
        )
    dimensions, exempt, non_person = dimension_lists(spec.line)
    return apply_suppression(spec, result, rules,
                             dimensions=dimensions, exempt=exempt,
                             non_person=non_person)


def list_metrics(line: str | None = None) -> list[MetricSpec]:
    """已註冊的指標。line 為 None 時回傳全部，否則只回那條線的。

    預設回全部是給「盤點所有指標」這種用途；真正要執行或產生文件時一定要
    指定 line，否則兩條線會互相污染。
    """
    specs = [REGISTRY[name] for name in sorted(REGISTRY)]
    if line is None:
        return specs
    return [s for s in specs if s.line == line]
