"""L2 聚合：request → turn / thread / user 三張表。

為什麼分三張：request 是原始粒度，但大部分問題不是問「幾次請求」。
agent 會把一個使用者動作展開成多次 request，用 request 當分母會嚴重灌水。
「打了幾句話」問 turn，「做了幾件事」問 thread，「多少人在用」問 user。

聚合前必須先分辨欄位在不同層級的行為，用錯會產生看起來合理的錯誤數字。
三類欄位分別宣告在下面的 CUMULATIVE / CONSTANT / ADDITIVE，不得混用。
"""

from __future__ import annotations

import logging

import pandas as pd

from src import config

logger = logging.getLogger(__name__)

TURN_PATH = config.DATA_AGG / "turn.parquet"
THREAD_PATH = config.DATA_AGG / "thread.parquet"
USER_PATH = config.DATA_AGG / "user.parquet"

# ---------------------------------------------------------------------------
# 欄位性質分類。這是整個 L2 最容易出錯的地方，所以列成明文常數。
# ---------------------------------------------------------------------------

# 累積型：thread 內單調成長，聚合時一律取 max。**絕不可 sum 或 mean。**
#
# 這些是「這次請求送出去的完整對話歷史長度」，每個 request 都把整段歷史重送一次。
# 對一個有 6 個 request 的 thread 取 sum 會得到
# 123+140+144+146+150+156 = 859，但實際訊息數是 156。
# 錯誤的版本不會報錯，只會讓所有「平均對話長度」類的指標膨脹好幾倍。
CUMULATIVE = ("message_count", "user_message_count", "assistant_message_count")

# 常數型：turn 或 thread 內固定不變，聚合時取 first，並驗證 nunique <= 1。
#
# 驗證不是形式：若某個 turn 內出現兩個不同的 reasoning_effort，
# 代表「turn 是一次使用者動作」這個分析單位的假設本身不成立，
# 而不是資料髒。那要回頭改分析單位，不是改聚合。
CONSTANT = (
    "reasoning_effort", "tool_count", "tool_types", "instructions_length",
    "text_verbosity", "prompt_cache_key", "model_requested",
)

# 可加總型：每個 request 各自獨立產生，sum 有意義。
ADDITIVE = (
    "prompt_tokens", "completion_tokens", "cached_tokens",
    "latency_ms", "sse_bytes",
)

# 錯誤的定義統一用 HTTP 狀態碼，而不是 error_type 是否為 null。
# 實測 status_code >= 400 有 86 筆、error_type 非 null 有 85 筆——
# 那筆 404 沒有 error 物件。用 error_type 判斷會漏掉它。
ERROR_STATUS_FLOOR = 400


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _join_unique(series: pd.Series) -> str | None:
    """不重複值排序後以逗號串接。全空回 None（不是空字串）。"""
    values = sorted({str(v) for v in series.dropna().unique()})
    return ",".join(values) if values else None


def _union_csv(series: pd.Series) -> str | None:
    """欄位本身已是逗號串接（如 tool_types），先拆再聯集。"""
    collected: set[str] = set()
    for value in series.dropna():
        collected.update(part for part in str(value).split(",") if part)
    return ",".join(sorted(collected)) if collected else None


def _duration_ms(first: pd.Series, last: pd.Series) -> pd.Series:
    return ((last - first).dt.total_seconds() * 1000).round().astype("Int64")


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """分母為 0 或缺失時回 NA，而不是 inf。

    inf 會一路流進 describe()/mean() 把整組統計污染成 inf，
    而且從欄位型別看不出來出過事。
    """
    num = numerator.astype("Float64")
    den = denominator.astype("Float64")
    return (num / den).where(den > 0)


def compaction_stats(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    """偵測 key 群組內的上下文壓縮，回傳 index=key 的 n_segments / has_compaction。

    判準是 message_count 依時間排序後**是否下降**。message_count 是累積型：
    正常情況下在同一個 turn 內只增不減，一旦變小就代表對話歷史被重置過
    （客戶端做了上下文壓縮，把前面的訊息換成摘要）。

    刻意不用「常數型欄位是否變動」來判斷。已實測有 4 個 turn 壓縮前後的
    model/reasoning_effort 設定完全相同，用常數欄位判斷會整批漏掉——
    設定沒變不代表歷史沒被砍掉，那是兩件事。

    n_segments = 下降次數 + 1，即這個 turn 實際包含幾段獨立的對話歷史。
    n_segments > 1 時，n_requests、token 加總都是跨段合併的結果。

    同一時間戳的多筆請求靠原始列序決定先後（sort 是穩定的）。這對計數
    幾乎沒有影響：壓縮是跨秒級的事件，不會發生在同一毫秒內的兩筆之間。
    """
    ordered = frame.sort_values([key, "ts_taipei"], kind="stable")
    counts = ordered["message_count"].astype("Float64")
    previous = counts.groupby(ordered[key]).shift(1)
    dropped = (counts < previous).fillna(False)

    per_group = dropped.groupby(ordered[key]).sum().astype("int64")
    return pd.DataFrame({
        "n_segments": per_group + 1,
        "has_compaction": per_group > 0,
    })


def check_constant_within(
    frame: pd.DataFrame, key: str, columns: tuple[str, ...], level: str
) -> dict[str, int]:
    """檢查常數型欄位在 key 群組內是否唯一。回傳 {欄位: 違反群組數}。

    nunique 預設忽略 NA：同一個 turn 內「有值」與「沒回報」並存不算違反，
    只有出現兩個不同的實際值才算。
    """
    violations: dict[str, int] = {}
    for column in columns:
        counts = frame.groupby(key, dropna=True)[column].nunique(dropna=True)
        bad = int((counts > 1).sum())
        if bad:
            violations[column] = bad
    if violations:
        logger.warning(
            "%s 層級的常數型欄位驗證失敗：%s"
            "——該欄位不是 %s 常數，分析單位的假設要重新檢查",
            level, violations, level,
        )
    else:
        logger.info("%s 層級常數型欄位驗證通過（%d 欄，違反 0 筆）",
                    level, len(columns))
    return violations


# ---------------------------------------------------------------------------
# 5-1 turn
# ---------------------------------------------------------------------------
def build_turn(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """粒度 turn_id。direct 客戶端沒有 turn，不進這張表。"""
    source = frame[frame["turn_id"].notna()].copy()
    source["is_error"] = source["status_code"] >= ERROR_STATUS_FLOOR

    violations = check_constant_within(source, "turn_id", CONSTANT, "turn")

    grouped = source.groupby("turn_id", dropna=True)
    turn = grouped.agg(
        thread_id=("thread_id", "first"),
        username=("username", "first"),
        client_type=("client_type", "first"),
        ts_first=("ts_taipei", "min"),
        ts_last=("ts_taipei", "max"),
        # 展開深度：一次使用者動作被 agent 拆成幾個 request
        n_requests=("request_id", "size"),
        # --- 可加總型 ---
        prompt_tokens_sum=("prompt_tokens", "sum"),
        completion_tokens_sum=("completion_tokens", "sum"),
        cached_tokens_sum=("cached_tokens", "sum"),
        latency_ms_sum=("latency_ms", "sum"),
        latency_ms_max=("latency_ms", "max"),
        # --- 累積型：取 max，不可 sum ---
        message_count_peak=("message_count", "max"),
        user_message_count_peak=("user_message_count", "max"),
        assistant_message_count_peak=("assistant_message_count", "max"),
        # --- 常數型：取 first（上面已驗證唯一性）---
        reasoning_effort=("reasoning_effort", "first"),
        tool_count=("tool_count", "first"),
        tool_types=("tool_types", "first"),
        # --- 集合型 ---
        model_family_set=("model_family", _join_unique),
        n_errors=("is_error", "sum"),
        has_usage_missing=("usage_missing", "any"),
    ).reset_index()

    turn = turn.merge(
        compaction_stats(source, "turn_id"), left_on="turn_id", right_index=True,
        how="left",
    )
    turn["duration_ms"] = _duration_ms(turn["ts_first"], turn["ts_last"])
    # 工具訊息數 = 總訊息 - 使用者訊息 - 助理訊息。三者都是累積型的 max，
    # 必須先各自取 max 再相減；先相減再聚合會得到不同的值。
    turn["tool_message_count"] = (
        turn["message_count_peak"]
        - turn["user_message_count_peak"]
        - turn["assistant_message_count_peak"]
    )
    turn["n_errors"] = turn["n_errors"].astype("Int64")

    ordered = [
        "turn_id", "thread_id", "username", "client_type",
        "ts_first", "ts_last", "duration_ms",
        "n_requests", "has_compaction", "n_segments",
        "prompt_tokens_sum", "completion_tokens_sum", "cached_tokens_sum",
        "latency_ms_sum", "latency_ms_max",
        "message_count_peak", "user_message_count_peak",
        "assistant_message_count_peak", "tool_message_count",
        "reasoning_effort", "tool_count", "tool_types",
        "model_family_set", "n_errors", "has_usage_missing",
    ]
    return turn[ordered], violations


# ---------------------------------------------------------------------------
# 5-2 thread
# ---------------------------------------------------------------------------
def build_thread(frame: pd.DataFrame) -> pd.DataFrame:
    """粒度 thread_id。"""
    source = frame[frame["thread_id"].notna()].copy()
    source["is_error"] = source["status_code"] >= ERROR_STATUS_FLOOR

    grouped = source.groupby("thread_id", dropna=True)
    thread = grouped.agg(
        username=("username", "first"),
        client_type=("client_type", "first"),
        # parent_thread_id 覆蓋率低，first 會取到 NaN；用 max 拿到非空值
        parent_thread_id=("parent_thread_id", "max"),
        ts_first=("ts_taipei", "min"),
        ts_last=("ts_taipei", "max"),
        n_turns=("turn_id", "nunique"),
        n_requests=("request_id", "size"),
        prompt_tokens_sum=("prompt_tokens", "sum"),
        completion_tokens_sum=("completion_tokens", "sum"),
        cached_tokens_sum=("cached_tokens", "sum"),
        message_count_peak=("message_count", "max"),
        user_message_count_peak=("user_message_count", "max"),
        assistant_message_count_peak=("assistant_message_count", "max"),
        model_family_set=("model_family", _join_unique),
        endpoint_set=("endpoint", _join_unique),
        tool_types_union=("tool_types", _union_csv),
        n_errors=("is_error", "sum"),
        error_codes=("error_code", _join_unique),
    ).reset_index()

    thread["duration_ms"] = _duration_ms(thread["ts_first"], thread["ts_last"])
    # 展開倍率：一件事平均被拆成幾個 request。
    # n_turns 可能為 0（有 thread_id 但整串都沒有 turn_id），此時回 NA 而非 inf。
    thread["expansion_ratio"] = _safe_ratio(thread["n_requests"], thread["n_turns"])
    thread["tool_message_ratio"] = _safe_ratio(
        thread["message_count_peak"]
        - thread["user_message_count_peak"]
        - thread["assistant_message_count_peak"],
        thread["message_count_peak"],
    )
    thread["has_tool_types"] = thread["tool_types_union"].notna()
    thread["n_errors"] = thread["n_errors"].astype("Int64")

    ordered = [
        "thread_id", "username", "client_type", "parent_thread_id",
        "ts_first", "ts_last", "duration_ms",
        "n_turns", "n_requests", "expansion_ratio",
        "prompt_tokens_sum", "completion_tokens_sum", "cached_tokens_sum",
        "message_count_peak", "user_message_count_peak",
        "assistant_message_count_peak", "tool_message_ratio",
        "model_family_set", "endpoint_set",
        "has_tool_types", "tool_types_union",
        "n_errors", "error_codes",
    ]
    return thread[ordered]


# ---------------------------------------------------------------------------
# 5-3 user
# ---------------------------------------------------------------------------
def load_registry() -> pd.DataFrame:
    """讀 ref/user_registry.csv。

    entry_year / dept_code 必須以字串讀入：它們是代碼不是數字，
    "08" 被讀成 8 之後就再也接不回原本的分組。
    """
    from src import identity

    if not identity.REGISTRY_PATH.exists():
        raise FileNotFoundError(
            f"找不到 {identity.REGISTRY_PATH}，請先執行 python -m src.run validate"
        )
    registry = pd.read_csv(
        identity.REGISTRY_PATH, encoding="utf-8-sig",
        dtype={"username": str, "account_type": str, "degree": str,
               "entry_year": str, "dept_code": str},
    )
    return registry[["username", "account_type", "degree", "entry_year", "dept_code"]]


def build_user(frame: pd.DataFrame, registry: pd.DataFrame) -> pd.DataFrame:
    """粒度 username。含 direct 客戶端的請求。"""
    source = frame.copy()
    source["is_error"] = source["status_code"] >= ERROR_STATUS_FLOOR

    grouped = source.groupby("username", dropna=True)
    user = grouped.agg(
        n_requests=("request_id", "size"),
        n_threads=("thread_id", "nunique"),
        n_turns=("turn_id", "nunique"),
        n_active_days=("date_taipei", "nunique"),
        ts_first=("ts_taipei", "min"),
        ts_last=("ts_taipei", "max"),
        prompt_tokens_sum=("prompt_tokens", "sum"),
        completion_tokens_sum=("completion_tokens", "sum"),
        cached_tokens_sum=("cached_tokens", "sum"),
        # 一個人可能同時用 codex 與 direct，所以是集合不是單值
        client_types=("client_type", _join_unique),
        endpoints=("endpoint", _join_unique),
        n_errors=("is_error", "sum"),
    ).reset_index()

    user["n_errors"] = user["n_errors"].astype("Int64")
    # 左接：以實際有流量的 username 為準。registry 若少了誰，屬性欄會是 NaN，
    # 而不是把那個人整列丟掉——人數對不上比屬性缺失嚴重得多。
    user = user.merge(registry, on="username", how="left")

    ordered = [
        "username", "account_type", "degree", "entry_year", "dept_code",
        "n_requests", "n_threads", "n_turns", "n_active_days",
        "ts_first", "ts_last",
        "prompt_tokens_sum", "completion_tokens_sum", "cached_tokens_sum",
        "client_types", "endpoints", "n_errors",
    ]
    return user[ordered].sort_values("username").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5-4 集中度
# ---------------------------------------------------------------------------
# 抑制規則的涵蓋範圍。指標按這裡列出的維度分組時，registry 會自動抑制其比例欄位。
#
# 只收「把人分群」的維度。判準是：這個維度的分組值本身是不是一個人群？
#   account_type / degree / entry_year / dept_code —— 是。「某系所佔 45%」
#     指向一群特定的人，母數又小，比例出去就有再識別風險。
#   client_type —— 是。它切的是「哪些人用 Codex、哪些人直呼」，
#     direct 這組實際上被一個人的腳本主導，其分位數就是在描述那個人。
#
# 刻意不收的維度：hour_taipei / weekday_taipei / endpoint / model_family /
# status_code / reasoning_effort。這些分的是**請求**不是**人**。抑制它們
# 只會把事實抹掉——「凌晨 3 點只有 2 個人在用」本身就是要報的事實，
# 不是需要隱藏的秘密；把那一格的比例清成 NA 讀者反而以為資料缺漏。
# 這些維度改為在指標輸出中一律附上 n_users，讓讀者自己判斷母數厚薄。
# registry.apply_suppression() 會強制檢查這件事。
CONCENTRATION_DIMENSIONS = (
    "account_type", "degree", "entry_year", "dept_code", "client_type",
)


def build_concentration(frame: pd.DataFrame, registry: pd.DataFrame) -> pd.DataFrame:
    """每個分組維度的人數與單人集中度。

    用途是讓後續每個分組統計都能自動附上警示：
    - below_min_group_size：母數太小，輸出比例會有再識別風險（config.MIN_GROUP_SIZE）
    - dominant：單一使用者佔該組流量過高，該組的「平均」其實是在描述一個人
      （config.DOMINANT_THRESHOLD）

    client_type 是請求層級屬性（同一人可能兩種都用），
    其餘四個是使用者層級屬性，靠 username 接上來。
    """
    joined = frame.merge(registry, on="username", how="left")
    rows: list[dict] = []

    for dimension in CONCENTRATION_DIMENSIONS:
        # groupby 預設丟棄 NA：degree/entry_year/dept_code 只對 student 有值，
        # staff/service 自然不出現在這些維度裡，這是正確行為。
        per_user = joined.groupby([dimension, "username"], dropna=True).agg(
            requests=("request_id", "size"),
            tokens=("total_tokens", "sum"),
        ).reset_index()

        for value, part in per_user.groupby(dimension, dropna=True):
            n_users = int(part["username"].nunique())
            n_requests = int(part["requests"].sum())
            tokens = float(part["tokens"].sum())
            top1_requests = float(part["requests"].max())
            top1_tokens = float(part["tokens"].max())
            request_share = top1_requests / n_requests if n_requests else None
            token_share = top1_tokens / tokens if tokens else None
            rows.append({
                "維度": dimension,
                "分組值": str(value),
                "n_users": n_users,
                "n_requests": n_requests,
                "n_tokens": int(tokens),
                "top1_user_share": round(request_share, 4) if request_share is not None else None,
                "top1_user_token_share": round(token_share, 4) if token_share is not None else None,
                "below_min_group_size": n_users < config.MIN_GROUP_SIZE,
                "dominant": bool(request_share is not None
                                 and request_share > config.DOMINANT_THRESHOLD),
            })

    concentration = pd.DataFrame(rows)
    return concentration.sort_values(
        ["維度", "n_requests"], ascending=[True, False]
    ).reset_index(drop=True)


def build_concentration_summary(concentration: pd.DataFrame) -> pd.DataFrame:
    """concentration.csv 的每維度彙總。

    這曾經是一個指標，已降級為 run metadata。理由：它的每一列都可以由
    concentration.csv 逐欄加總還原，零新增資訊；而且它描述的是**我們自己的
    抑制規則**，不是使用者行為。指標目錄應該只放後者。
    """
    if concentration.empty:
        return pd.DataFrame(columns=["維度"])

    rows = []
    for dimension, part in concentration.groupby("維度"):
        flagged = part["below_min_group_size"] | part["dominant"]
        usable = part[~flagged]
        rows.append({
            "維度": dimension,
            "n_groups": int(len(part)),
            "n_usable": int(len(usable)),
            "n_below_min_group_size": int(part["below_min_group_size"].sum()),
            "n_dominant": int(part["dominant"].sum()),
            "可用分組值": ",".join(map(str, usable["分組值"].tolist())) or "（無）",
            "全部分組皆可出比例": bool(len(usable) == len(part)),
        })
    return pd.DataFrame(rows).sort_values(
        ["全部分組皆可出比例", "n_usable"], ascending=[False, False]
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run(run_id: str) -> dict:
    from src import schema

    frame = schema.load_dataset()
    registry = load_registry()
    config.DATA_AGG.mkdir(parents=True, exist_ok=True)
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    turn, violations = build_turn(frame)
    thread = build_thread(frame)
    user = build_user(frame, registry)
    concentration = build_concentration(frame, registry)
    concentration_summary = build_concentration_summary(concentration)

    turn.to_parquet(TURN_PATH, index=False)
    thread.to_parquet(THREAD_PATH, index=False)
    user.to_parquet(USER_PATH, index=False)
    concentration_path = run_dir / "concentration.csv"
    concentration.to_csv(concentration_path, index=False, encoding="utf-8-sig")
    summary_path = run_dir / "concentration_summary.csv"
    concentration_summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    n_compacted = int(turn["has_compaction"].sum())
    logger.info("--- L2 聚合報告 ---")
    logger.info("turn   %5d 列 → %s", len(turn), TURN_PATH)
    logger.info("  其中發生上下文壓縮的 turn：%d（n_segments 最大 %d）",
                n_compacted, int(turn["n_segments"].max()))
    logger.info("thread %5d 列 → %s", len(thread), THREAD_PATH)
    logger.info("user   %5d 列 → %s", len(user), USER_PATH)
    logger.info("集中度 %5d 列 → %s", len(concentration), concentration_path)
    logger.info("集中度彙總 %3d 列 → %s（run metadata，非指標）",
                len(concentration_summary), summary_path)

    flagged = concentration[concentration["below_min_group_size"]
                            | concentration["dominant"]]
    logger.info("需警示的分組 %d/%d（母數不足或單人主導）",
                len(flagged), len(concentration))
    for row in flagged.itertuples():
        marks = []
        if row.below_min_group_size:
            marks.append(f"母數 {row.n_users} < {config.MIN_GROUP_SIZE}")
        if row.dominant:
            marks.append(f"單人佔 {100*row.top1_user_share:.1f}%")
        logger.info("  %s=%s：%s", row.維度, row.分組值, "、".join(marks))

    return {
        "turn_rows": len(turn),
        "turns_with_compaction": n_compacted,
        "thread_rows": len(thread),
        "user_rows": len(user),
        "concentration_rows": len(concentration),
        "constant_violations": violations,
        "concentration_file": str(concentration_path),
        "concentration_summary_file": str(summary_path),
    }
