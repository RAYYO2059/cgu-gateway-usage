"""專案層級的路徑與常數定義。

所有路徑都由本檔案位置往上推導，因此不論從哪個工作目錄執行都能正確解析。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 路徑
# ---------------------------------------------------------------------------
# config.py 位於 <root>/src/config.py，往上兩層即專案根目錄。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
DATA_RAW = DATA_DIR / "00_raw"          # 原始 JSON，一檔一請求
DATA_REQUEST = DATA_DIR / "01_request"  # 攤平後的請求級 parquet
DATA_AGG = DATA_DIR / "02_agg"          # 聚合結果
MANIFEST_DIR = DATA_DIR / "_manifest"   # 每次處理的檔案清單／雜湊

REF_DIR = PROJECT_ROOT / "ref"          # 帳號對照表等敏感參照資料
RUNS_DIR = PROJECT_ROOT / "runs"        # 每次執行的快照
DOCS_DIR = PROJECT_ROOT / "docs"

# 需要 ensure_dirs() 建立的目錄，順序即建立順序。
_MANAGED_DIRS = (
    DATA_DIR,
    DATA_RAW,
    DATA_REQUEST,
    DATA_AGG,
    MANIFEST_DIR,
    REF_DIR,
    RUNS_DIR,
    DOCS_DIR,
)

# ---------------------------------------------------------------------------
# 常數
# ---------------------------------------------------------------------------
PIPELINE_VERSION = "0.1.0"

# 原始日誌時間戳轉換成本地時間時使用的時區。
TIMEZONE = "Asia/Taipei"

MIN_GROUP_SIZE = 10      # 小於此母數的分組不輸出比例，避免再識別
DOMINANT_THRESHOLD = 0.30  # 單一使用者佔某群流量超過此比例即標記

# run_id 形如 2026-08-06T1430_r001
RUN_ID_FORMAT = "%Y-%m-%dT%H%M"
_RUN_ID_RE = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})T\d{4}_r(?P<seq>\d{3,})$")


def ensure_dirs() -> None:
    """建立專案所需的全部目錄，已存在則略過。"""
    for directory in _MANAGED_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def new_run_id(now: datetime | None = None) -> str:
    """產生新的 run id，例如 ``2026-08-06T1430_r001``。

    時間戳取本地時間；序號掃描 ``runs/`` 底下同一天已存在的目錄後遞增，
    因此同一天內多次執行會得到 r001、r002……。
    """
    now = now or datetime.now()
    stamp = now.strftime(RUN_ID_FORMAT)
    day = now.strftime("%Y-%m-%d")

    highest = 0
    if RUNS_DIR.is_dir():
        for entry in RUNS_DIR.iterdir():
            if not entry.is_dir():
                continue
            matched = _RUN_ID_RE.match(entry.name)
            if matched and matched.group("day") == day:
                highest = max(highest, int(matched.group("seq")))

    return f"{stamp}_r{highest + 1:03d}"
