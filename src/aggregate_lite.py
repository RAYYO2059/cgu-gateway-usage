"""lite 的 L2 聚合：user_lite.parquet 與 concentration_lite.csv。

與 clean 的 aggregate.py 並行，但**共用集中度與抑制的計算**——
``build_concentration()`` 是同一個函式，只是傳入不同的人層級鍵與維度清單。

共用而不是各寫一份，是因為抑制邏輯分岔的失敗模式特別糟：兩份實作只要有
一份被改而另一份沒有，兩條線的隱私保護強度就會不一致，而且不會有任何錯誤
訊息——只會有一邊的數字悄悄流出去。

lite 只有 request 與 user 兩層，沒有 turn/thread：那兩層要靠 turn_id 的
父子關係組出來，而 lite 匯出只有 thread_id、沒有 turn_id。
"""

from __future__ import annotations

import logging

import pandas as pd

from src import college, config, identity, identity_lite

logger = logging.getLogger(__name__)

USER_LITE_PATH = config.DATA_AGG_LITE / "user_lite.parquet"

# lite 的兩份維度清單。與 clean 的模組層級常數**分開**宣告，不是覆寫它們：
# 兩條線的維度不同，共用一份會讓其中一條線的維度被誤判成「未登記」。
#
#   要抑制的：這些維度把人分群
#   明確豁免的：這些維度分的是請求
#   兩份都沒有的 → apply_suppression 會納入抑制並警告要求登記
CONCENTRATION_DIMENSIONS_LITE = (
    "account_type", "unit", "college", "degree", "entry_year", "dept_code",
    # request_style 放抑制清單而不是豁免，理由與 clean 把 client_type 放進
    # 抑制清單相同（見 aggregate.py 的 CONCENTRATION_DIMENSIONS 註解）：
    # 它雖然是請求層級屬性、同一人可能兩種都用，但它仍然會把人分群——
    # 「只用 responses 的那群人」是一群人，不是一群請求。
    "request_style",
)
EXEMPT_DIMENSIONS_LITE = (
    "endpoint",       # API 端點
    "model_family",   # 模型
    "provider",       # openai / ollama
    "hour_taipei",    # 一天的第幾小時
    "status_code",    # HTTP 狀態碼
)

UNIT_STAFF = "教職員"
UNIT_SERVICE = "服務憑證"

UNIT_TYPE_COLLEGE = "學院"
UNIT_TYPE_STAFF = "教職員"
UNIT_TYPE_SERVICE = "服務憑證"


def load_registry() -> pd.DataFrame:
    """讀 ref/lite_user_registry.csv。

    entry_year / dept_code 必須以字串讀入：它們是代碼不是數字，
    "08" 被讀成 8 之後就再也接不回原本的分組。
    """
    if not identity_lite.REGISTRY_PATH.exists():
        raise FileNotFoundError(
            f"找不到 {identity_lite.REGISTRY_PATH}，"
            "請先執行 python -m src.identity_lite"
        )
    return pd.read_csv(
        identity_lite.REGISTRY_PATH,
        encoding="utf-8-sig",
        dtype={"entry_year": "string", "dept_code": "string",
               "anonymous_user_id": "string", "user_account": "string",
               "account_type": "string", "degree": "string"},
    )


def assign_unit(registry: pd.DataFrame,
                entries: list | None = None) -> pd.DataFrame:
    """替每個人加上 unit / unit_type / college 三欄。

    - student → unit = 所屬學院（查不到就是 college.UNMAPPED），unit_type = 學院
    - staff   → unit = "教職員"
    - service → unit = "服務憑證"

    **``(未對應)`` 刻意留在 unit_type="學院" 這一類裡。** 它是「學生但歸不到
    學院」，不是第四種身分——把它另分一類會讓「學院這個維度覆蓋多少人」這個
    問題失去分母，覆蓋缺口就從表面上消失了。要看缺口有多大請看
    unmapped_dept_codes_lite 與 unit_coverage_lite。
    """
    entries = entries if entries is not None else college.load_mapping()
    out = registry.copy()

    def one(row) -> tuple[str, str, str | None]:
        account_type = row["account_type"]
        if account_type == identity.TYPE_STAFF:
            return UNIT_STAFF, UNIT_TYPE_STAFF, None
        if account_type == identity.TYPE_SERVICE:
            return UNIT_SERVICE, UNIT_TYPE_SERVICE, None
        resolved = college.dept_to_college(row["dept_code"], row["entry_year"],
                                           entries)
        return resolved, UNIT_TYPE_COLLEGE, resolved

    assigned = [one(row) for _, row in out.iterrows()]
    out["unit"] = [a[0] for a in assigned]
    out["unit_type"] = [a[1] for a in assigned]
    # college 只有學生有值。教職員與服務憑證留 NA，不要填 "無"——
    # groupby 會把 "無" 當成一個真的學院。
    out["college"] = [a[2] for a in assigned]
    return out


def build_user_lite(frame: pd.DataFrame, registry: pd.DataFrame) -> pd.DataFrame:
    """粒度 anonymous_user_id。"""
    grouped = frame.groupby("anonymous_user_id", dropna=True)
    usage = grouped.agg(
        n_requests=("request_id", "size"),
        first_seen=("ts_taipei", "min"),
        last_seen=("ts_taipei", "max"),
        prompt_tokens=("prompt_tokens", "sum"),
        completion_tokens=("completion_tokens", "sum"),
        cached_tokens=("cached_tokens", "sum"),
    ).reset_index()

    # registry 自己也有 n_requests / first_seen / last_seen（identity_lite 寫的），
    # 但這裡一律以請求表重算為準：registry 可能是上一批資料產生的，而 merge
    # 撞名只會安靜地變成 _x/_y，兩個都留著遲早有人取到過期的那一欄。
    identity_only = assign_unit(registry).drop(
        columns=[c for c in usage.columns if c != "anonymous_user_id"],
        errors="ignore")
    user = identity_only.merge(usage, on="anonymous_user_id", how="left")
    columns = [
        "anonymous_user_id", "user_account", "account_type",
        "degree", "entry_year", "dept_code", "college", "unit", "unit_type",
        "n_requests", "first_seen", "last_seen",
        "prompt_tokens", "completion_tokens", "cached_tokens",
    ]
    return user[columns].sort_values("anonymous_user_id").reset_index(drop=True)


def build_concentration_lite(frame: pd.DataFrame,
                             user: pd.DataFrame) -> pd.DataFrame:
    """lite 的集中度。呼叫共用的 build_concentration()。"""
    from src.aggregate import build_concentration

    # 只餵身分欄位，不餵用量欄位：user_lite 有 prompt_tokens 等欄，
    # 與請求表同名，merge 之後會變成 prompt_tokens_x/_y，
    # 而 build_concentration 要讀的 total_tokens 就再也對不上。
    identity_columns = [
        "anonymous_user_id", "account_type", "degree", "entry_year",
        "dept_code", "college", "unit", "unit_type",
    ]
    return build_concentration(
        frame, user[identity_columns],
        user_key="anonymous_user_id",
        dimensions=CONCENTRATION_DIMENSIONS_LITE,
    )


def run(run_id: str) -> dict:
    from src import extract_lite

    frame = extract_lite.load_dataset()
    registry = load_registry()
    config.DATA_AGG_LITE.mkdir(parents=True, exist_ok=True)
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    user = build_user_lite(frame, registry)
    concentration = build_concentration_lite(frame, user)

    user.to_parquet(USER_LITE_PATH, index=False)
    concentration_path = run_dir / "concentration_lite.csv"
    concentration.to_csv(concentration_path, index=False, encoding="utf-8-sig")

    logger.info("--- L2 lite 聚合報告 ---")
    logger.info("user_lite %5d 列 → %s", len(user), USER_LITE_PATH)
    logger.info("  unit_type 分布 %s", user["unit_type"].value_counts().to_dict())
    logger.info("  unit 分布 %s", user["unit"].value_counts().to_dict())
    logger.info("集中度 %5d 列 → %s", len(concentration), concentration_path)

    flagged = concentration[concentration["below_min_group_size"]
                            | concentration["dominant"]]
    logger.info("需警示的分組 %d/%d（母數不足或單人主導）",
                len(flagged), len(concentration))
    for row in flagged.itertuples():
        marks = []
        if row.below_min_group_size:
            marks.append(f"母數 {row.n_users} < {config.MIN_GROUP_SIZE}")
        if row.dominant:
            marks.append(f"單人佔 {100 * row.top1_user_share:.1f}%")
        logger.info("  %s=%s：%s", row.維度, row.分組值, "、".join(marks))

    return {"n_users": len(user), "n_concentration": len(concentration)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config.ensure_dirs()
    run(config.new_run_id())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
