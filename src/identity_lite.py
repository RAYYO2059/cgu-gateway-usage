"""lite 的身分解析：由 L1 lite 輸出建立 ref/lite_user_registry.csv。

**人層級鍵是 anonymous_user_id，不是 username，也不是 user_account。**
lite 沒有 username 這個欄位；clean 的 identity.py 以 username 為人鍵，兩邊
不共用 registry，也不該互相對照（uid 與 username 之間沒有已知的對應關係）。

已在 120,520 筆全量 lite 資料上驗證的前提（不在這裡重新推導）：

- **anonymous_user_id → user_account 是單值的。** 357 個 uid 裡，
  有多個帳號的是 **0 組**。每個 uid 剛好掛一個 user_account。
- **user_account → anonymous_user_id 是一對多。** 119 個相異帳號裡，
  **39 組**對應到多於一個 uid，最嚴重的 ``D000020XXX`` 底下有 **29 個** uid。
  （clean 的 7 月資料裡最嚴重是 11 個，lite 這批更糟。）
- 因此 **anonymous_user_id 是唯一的人層級鍵**；user_account 只能當屬性來源
  ——身分別、學位、入學學年、系代號——**不能當識別碼**。拿帳號當人用，
  最壞的情況會把 29 個不同的人算成 1 個。

碰撞的來源是遮罩：帳號末三碼被抹成 XXX，序號不同的人因此塌成同一個字串。
所以碰撞**只發生在帳號那個方向**。

**uid → 系代號 → 學院這條路徑不受碰撞影響。** 同一個遮罩帳號底下的那 29 個
人，共用的正是遮罩前綴所編碼的那組屬性（學位＋入學學年＋系代號），所以由
uid 取到帳號、再由帳號取到系代號，結果仍然是單值且正確的。壞掉的是反方向：
由帳號回推是誰。學院層級的統計安全，個人層級的識別不安全。

帳號的解析邏輯直接呼叫 identity.classify_account()，不重寫——那套規則
（用長度／數字位數區分學生與教職員，而不是用首字母）是 clean 那邊踩過坑
才定下來的，抄一份到這裡只會讓兩邊哪天漂開。

輸出含遮罩帳號，落在 ref/ 底下，由 .gitignore 排除。
"""

from __future__ import annotations

import logging

import pandas as pd

from src import config, identity

logger = logging.getLogger(__name__)

REGISTRY_PATH = config.REF_DIR / "lite_user_registry.csv"

REGISTRY_COLUMNS = (
    "anonymous_user_id", "user_account", "account_type",
    "degree", "entry_year", "dept_code",
    "n_requests", "first_seen", "last_seen",
)

UID_COLUMN = "anonymous_user_id"
ACCOUNT_COLUMN = "user_account"


def build_registry(frame: pd.DataFrame) -> pd.DataFrame:
    """一列一個 anonymous_user_id。"""
    rows: list[dict] = []

    for uid, part in frame.groupby(UID_COLUMN, sort=True):
        accounts = part[ACCOUNT_COLUMN].dropna()
        counts = accounts.value_counts()

        if len(counts) == 0:
            account = None
            attrs = {"account_type": None, "degree": None,
                     "entry_year": None, "dept_code": None}
        else:
            # 前提是單值（見模組 docstring）。真的出現多值時取請求數最多的那個，
            # 並留下 warning——不靜靜取第一個，那樣沒人會知道前提被打破了。
            if len(counts) > 1:
                logger.warning(
                    "uid %s 對應到 %d 個 user_account %s，取請求數最多的 %s；"
                    "identity_lite 的單值前提已被打破，請人工確認",
                    uid, len(counts), list(counts.index), counts.index[0])
            account = str(counts.index[0])
            attrs = identity.classify_account(account)

        rows.append({
            "anonymous_user_id": uid,
            "user_account": account,
            "account_type": attrs["account_type"],
            "degree": attrs["degree"],
            "entry_year": attrs["entry_year"],
            "dept_code": attrs["dept_code"],
            "n_requests": len(part),
            "first_seen": part["ts_taipei"].min(),
            "last_seen": part["ts_taipei"].max(),
        })

    registry = pd.DataFrame(rows, columns=list(REGISTRY_COLUMNS))
    return registry.sort_values(UID_COLUMN).reset_index(drop=True)


def account_collisions(frame: pd.DataFrame) -> pd.DataFrame:
    """一個 user_account 對應到多個 uid 的組別，由多到少排序。

    這不是給下游用的，是給人看的：它量化「遮罩帳號不能當人用」這件事有多嚴重，
    而嚴重程度會隨著每一批新資料改變。
    """
    pairs = frame[[ACCOUNT_COLUMN, UID_COLUMN]].dropna().drop_duplicates()
    counts = pairs.groupby(ACCOUNT_COLUMN)[UID_COLUMN].nunique()
    counts = counts[counts > 1].sort_values(ascending=False)
    return counts.rename("n_uids").reset_index()


def save_registry(registry: pd.DataFrame) -> None:
    config.REF_DIR.mkdir(parents=True, exist_ok=True)
    registry.to_csv(REGISTRY_PATH, index=False, encoding="utf-8-sig", lineterminator="\n")


def run(frame: pd.DataFrame | None = None) -> pd.DataFrame:
    if frame is None:
        from src import extract_lite

        frame = extract_lite.load_dataset()

    registry = build_registry(frame)
    save_registry(registry)

    dist = registry["account_type"].value_counts(dropna=False).to_dict()
    collisions = account_collisions(frame)
    logger.info("lite_user_registry：%d 列 → %s", len(registry), REGISTRY_PATH)
    logger.info("  account_type 分布（以人計）%s", dist)
    logger.info("  相異 user_account %d 個",
                frame[ACCOUNT_COLUMN].dropna().nunique())
    if len(collisions):
        logger.warning(
            "  遮罩帳號碰撞：%d 組帳號底下有多於一個 uid，最多 %d 個（%s）",
            len(collisions), int(collisions["n_uids"].max()),
            collisions.iloc[0][ACCOUNT_COLUMN])
    return registry


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
