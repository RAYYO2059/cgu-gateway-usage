"""身分解析：由 L1 輸出建立 ref/user_registry.csv。

已驗證的前提（不在這裡重新推導）：

- username 是 gateway 指派的人層級識別碼，格式 userNNNN，依首次出現時間遞增。
  它跨作業系統、跨客戶端產品、跨憑證都保持不變，所以**它才是使用者鍵**。
- user_account 的最後三碼被遮罩成 XXX。可見前綴不足以分辨人：
  最嚴重的一個 masked account 底下有 11 個不同的 username。
  因此 user_account 只能當屬性（身分別、系所、學年），不能當人。
- username → user_account 不是函數關係。實測有 1 個例外：
  同一人同時持有學士與博士帳號，在同一個 Codex thread 內交替使用兩組憑證。
  對這種情況兩個帳號都保留，不強制歸一。

輸出含帳號資訊，落在 ref/ 底下，由 .gitignore 排除。
"""

from __future__ import annotations

import logging
import re

import pandas as pd

from src import config

logger = logging.getLogger(__name__)

REGISTRY_PATH = config.REF_DIR / "user_registry.csv"

REGISTRY_COLUMNS = (
    "username", "account_type", "account_masked",
    "degree", "entry_year", "dept_code",
    "first_seen", "last_seen", "n_requests", "n_accounts",
)

# 學生：單一字母 + 4 碼數字 + XXX（local part 長 8），如 B1229XXX、D1327XXX
# 教職員：D + 6 碼數字 + XXX（local part 長 10），如 D000020XXX
#
# 兩者都可能以 D 開頭，所以**用長度／數字位數區分，不能用首字母判斷**。
# 用首字母的話，博士生 D1327XXX 會被誤判成教職員，
# 而博士生正好是使用量最高的族群之一，誤判會讓「學生 vs 教職員」的
# 用量分布整組偏移。
_RE_STUDENT = re.compile(r"^([A-Za-z])(\d{2})(\d{2})XXX$")
_RE_STAFF = re.compile(r"^D\d{6}XXX$")

TYPE_STUDENT = "student"
TYPE_STAFF = "staff"
TYPE_SERVICE = "service"


def local_part(account: str) -> str:
    return account.split("@", 1)[0]


def classify_account(account: str) -> dict[str, str | None]:
    """回傳單一 user_account 的身分別與衍生欄位。"""
    local = local_part(account)

    matched = _RE_STUDENT.match(local)
    if matched:
        degree, entry_year, dept_code = matched.groups()
        return {
            "account_type": TYPE_STUDENT,
            "degree": degree.upper(),
            "entry_year": entry_year,   # 民國學年前 2 碼
            "dept_code": dept_code,
        }

    if _RE_STAFF.match(local):
        # 教職員編號沒有學位／學年／系所語意，一律 None。
        # 填 "" 或 "NA" 會讓下游的 groupby 多出一個假類別。
        return {
            "account_type": TYPE_STAFF,
            "degree": None, "entry_year": None, "dept_code": None,
        }

    # link-*、open* 等服務憑證。這些不是人，且本來就沒有遮罩，原樣輸出。
    return {
        "account_type": TYPE_SERVICE,
        "degree": None, "entry_year": None, "dept_code": None,
    }


def build_registry(frame: pd.DataFrame) -> pd.DataFrame:
    """一列一個 username。"""
    rows: list[dict] = []

    for username, part in frame.groupby("username", sort=True):
        # 帳號排序：請求數多的在前，同數量以帳號字串排序，確保輸出可重現。
        counts = part.groupby("user_account").size().sort_values(
            ascending=False, kind="mergesort")
        accounts = sorted(counts.index, key=lambda a: (-counts[a], a))

        classified = [(a, classify_account(a)) for a in accounts]
        # account_type 取第一個非 service 的值；全為 service 才標 service。
        primary = next(
            (c for _, c in classified if c["account_type"] != TYPE_SERVICE),
            classified[0][1],
        )

        rows.append({
            "username": username,
            "account_type": primary["account_type"],
            "account_masked": ";".join(accounts),
            "degree": primary["degree"],
            "entry_year": primary["entry_year"],
            "dept_code": primary["dept_code"],
            "first_seen": part["ts_taipei"].min(),
            "last_seen": part["ts_taipei"].max(),
            "n_requests": len(part),
            "n_accounts": len(accounts),
        })

    registry = pd.DataFrame(rows, columns=list(REGISTRY_COLUMNS))
    return registry.sort_values("username").reset_index(drop=True)


def save_registry(registry: pd.DataFrame) -> None:
    config.REF_DIR.mkdir(parents=True, exist_ok=True)
    registry.to_csv(REGISTRY_PATH, index=False, encoding="utf-8-sig")


def run(frame: pd.DataFrame | None = None) -> pd.DataFrame:
    if frame is None:
        from src import schema
        frame = schema.load_dataset()

    registry = build_registry(frame)
    save_registry(registry)

    dist = registry["account_type"].value_counts().to_dict()
    multi = int((registry["n_accounts"] > 1).sum())
    logger.info("user_registry：%d 列 → %s", len(registry), REGISTRY_PATH)
    logger.info("  account_type 分布 %s", dist)
    logger.info("  持有多個帳號的 username %d 個", multi)
    return registry
