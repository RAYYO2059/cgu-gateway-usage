"""專案層級的路徑與常數定義。

所有路徑都由本檔案位置往上推導，因此不論從哪個工作目錄執行都能正確解析。
"""

from __future__ import annotations

import os
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

# --- lite 資料流 -----------------------------------------------------------
# lite 是另一套上游匯出（schema ai_platform_request 1.1-lite），欄位路徑、
# 身分鍵、時間戳語意都與 clean 不同，**兩者不可混在同一組目錄**。
#
# 特別是原始檔：clean 的 extract.py 用 DATA_RAW.rglob("*.json") 掃檔，
# 只要 lite 的 json 落進 data/00_raw/ **底下任何一層**就會被撿走，然後用 clean
# 的欄位路徑去解析——產出 12 萬列幾乎全 null 的資料，而 request_id 剛好在
# 兩套 schema 裡都是頂層同名欄位，會有值，schema 驗證未必攔得住。
#
# rglob 是從 DATA_RAW 往下遞迴，所以危險的範圍就是 data/00_raw/ 底下；
# **兄弟目錄掃不到**，這也是 00_raw_lite 取這個位置的理由——名字看起來相鄰，
# 實際上完全在 rglob 的範圍之外。
DATA_RAW_LITE = DATA_DIR / "00_raw_lite"    # lite 原始 JSON，00_raw 的兄弟目錄
DATA_REQUEST_LITE = DATA_DIR / "01_request_lite"
DATA_AGG_LITE = DATA_DIR / "02_agg_lite"
MANIFEST_LITE = DATA_DIR / "_manifest_lite"

LITE_RAW_ENV = "CGU_LITE_RAW"

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
    DATA_RAW_LITE,
    DATA_REQUEST_LITE,
    DATA_AGG_LITE,
    MANIFEST_LITE,
)

# ---------------------------------------------------------------------------
# 常數
# ---------------------------------------------------------------------------
PIPELINE_VERSION = "0.5.0"

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


def lite_raw_dir() -> Path:
    """lite 原始 JSON 的根目錄。

    預設是 data/00_raw_lite/。環境變數 CGU_LITE_RAW 可以覆寫——這批資料
    約 680 MB，硬碟空間不夠時可以指到外接碟或別的磁碟。

    無論用哪一個，都會擋掉指向 data/00_raw/ 底下的路徑（含任何子目錄）：
    那是 clean 的 extract.py 用 rglob 遞迴掃描的範圍。
    """
    override = os.environ.get(LITE_RAW_ENV, "").strip()
    path = Path(override).expanduser() if override else DATA_RAW_LITE
    source = f"環境變數 {LITE_RAW_ENV}" if override else "預設值"

    # 防呆：擋掉 data/00_raw 本身與它底下的任何一層。
    # 用 Path.relative_to 而不是字串前綴比對——字串比對會把
    # data/00_raw_lite 誤判成 data/00_raw 的子目錄（前綴剛好相同），
    # 而那正是本專案實際採用的合法位置。
    resolved = path.resolve()
    try:
        inside = resolved.relative_to(DATA_RAW.resolve())
    except ValueError:
        inside = None
    if inside is not None:
        raise ValueError(
            f"{source} 指向 {path}，位於 {DATA_RAW} 底下"
            f"（相對位置 {inside}）。\n"
            "clean 的 extract.py 用 DATA_RAW.rglob('*.json') 遞迴掃描那個目錄，"
            "會把 lite 的 json 一起撿走，再用 clean 的欄位路徑解析，"
            "產出整批幾乎全 null 的資料——而 request_id 在兩套 schema 裡都是"
            "頂層同名欄位、會有值，schema 驗證未必攔得住。\n"
            f"請改放到 {DATA_RAW_LITE}（00_raw 的兄弟目錄，不在 rglob 範圍內）"
            "或 repo 之外。"
        )

    if not path.is_dir():
        raise NotADirectoryError(
            f"{source} 指向 {path}，但它不是一個目錄。\n"
            "它應該是 lite 匯出解壓後的根目錄，底下是 YYYY-MM-DD 的日期資料夾。\n"
            f"  預設位置：    {DATA_RAW_LITE}\n"
            "  或以環境變數覆寫：\n"
            f"    PowerShell： $env:{LITE_RAW_ENV} = 'D:\\path\\to\\lite_raw'\n"
            f"    bash：       export {LITE_RAW_ENV}=/path/to/lite_raw"
        )
    return path


def new_run_id(now: datetime | None = None) -> str:
    """產生新的 run id 並**當場建立它的 runs/ 目錄**，例如 ``2026-08-06T1430_r001``。

    時間戳取本地時間；序號掃描 ``runs/`` 底下同一天已存在的目錄後遞增，
    因此同一天內多次執行會得到 r001、r002……。

    為什麼要順手建目錄
    ------------------
    序號是從「已存在的目錄」推出來的，所以**產生 id 與佔用號碼是同一件事**，
    不是兩件。目錄若拖到執行尾聲才建（原本各階段都是寫檔時才順手 mkdir），
    中途死掉的執行就從來不會佔到號碼，下一次會拿到同一個序號。

    這是必然碰撞不是機率碰撞：實測同一分鐘內連續呼叫兩次，拿到的是字面上
    完全相同的 run_id（``2026-08-26T0400_r001`` 兩次），連時間戳都一樣。
    lite 那條線已經因此吃過虧——12 萬筆的前四次抽取都被逾時砍掉，連續四次
    都拿到 r003，只靠分鐘精度的時間戳僥倖沒撞在一起。

    對 clean 而言目前是條件性的危險：它不分塊寫入，一個 run 只寫一次檔，
    所以撞了也不會互相覆蓋。但哪天 clean 的資料量大到要分塊（例如補進
    8 月的 clean 版本），``part-<run_id>-*.parquet`` 就會靜默互相覆蓋，
    而那時候沒有人會記得這個洞在這裡。

    副作用是好的：中途死掉會留下一個沒有 ``run_manifest.json`` 的空 run
    目錄，那本身就是「跑了但沒跑完」的訊號。
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

    run_id = f"{stamp}_r{highest + 1:03d}"
    (RUNS_DIR / run_id).mkdir(parents=True, exist_ok=True)
    return run_id
