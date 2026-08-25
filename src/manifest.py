"""每次執行的自我描述檔：``runs/<run_id>/run_manifest.json``。

快照原本只有輸出檔，要判斷「這個 run 是不是完整的」必須反推檔案組成：
有沒有 ``metrics/``、裡面幾個 csv、有沒有 ``concentration.csv``。反推會錯，
而且判準會隨著指標增減而失效——指標從 20 個變成 19 個之後，「csv 數 = 20」
這條規則就把所有新的成功 run 判成不完整。

所以讓快照自己說：跑了哪些階段、成功還是半成功、當時的母數是多少。
檔案裡刻意不放推導得出來的東西（例如指標清單，那是 metrics_summary.csv 的事），
只放「事後無法從輸出反推」的執行事實。

本檔含時間戳，因此逐次執行的內容必然不同。這不影響 ``--publish`` 的冪等性：
manifest 落在 ``runs/`` 底下，不進 ``docs/`` 也不進版控。

run_id 的唯一性從哪裡來
-----------------------
序號（``_rNNN``）是 ``config.new_run_id()`` 掃描 ``runs/`` 底下同日已存在的
目錄推出來的，所以**「產生 id」與「佔用號碼」必須是同一個動作**。目錄若拖到
執行尾聲才建立，中途死掉的執行就不會佔到號碼，下一次會拿到相同的 run_id
——而且是必然碰撞：實測同一分鐘內連續呼叫兩次會得到字面上完全相同的字串。

因此 ``new_run_id()`` 產生 id 時就會把目錄建起來。看到一個沒有
``run_manifest.json`` 的空 run 目錄不要覺得奇怪，那正是「跑了但沒跑完」。

這件事對「快照能不能被信任」是前提：兩個不同的執行若共用 run_id，它們的輸出
會寫進同一個目錄互相覆蓋，而 manifest 只會留下後寫的那一份——快照看起來完整，
內容卻是兩次執行的混合。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src import config

logger = logging.getLogger(__name__)

FILENAME = "run_manifest.json"

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


def _now() -> datetime:
    """帶時區位移的本地時間。裸 datetime 在跨機器比對時無法判讀。"""
    return datetime.now().astimezone()


def _count_requests(path: Path | None = None) -> int | None:
    """請求級資料表目前的列數。只讀 parquet footer，不載入資料。

    記的是「這次執行所面對的資料集有多大」，不是「這次抽取處理了幾列」——
    後者在增量執行時是 0，拿來描述快照會誤導。

    path 預設是 clean 的 01_request。lite 那條線要傳自己的目錄進來——
    寫死 clean 的路徑會讓 lite 的 run_manifest 記上 clean 的母數，
    一份自我描述檔記了別條線的數字，比沒有紀錄更糟。
    """
    path = path or config.DATA_REQUEST
    if not path.is_dir():
        return None
    try:
        import pyarrow.dataset as ds

        dataset = ds.dataset(path, format="parquet", partitioning="hive")
        return int(dataset.count_rows())
    except Exception as exc:  # 缺 pyarrow、分區壞掉、空目錄
        logger.debug("無法計算請求列數：%s", exc)
        return None


@dataclass
class RunManifest:
    """收集一次執行的事實，結束時寫成 json。

    刻意做成可變的收集器而不是最後才組裝：階段是在執行過程中才知道的，
    而且失敗的執行也要留下記錄——只在成功時才寫 manifest 的話，
    「哪個 run 掛了」這個問題又會退回去看檔案組成。
    """

    command: str
    run_id: str | None = None
    stages: list[str] = field(default_factory=list)
    published: bool = False
    n_metrics_run: int = 0
    n_metrics_failed: int = 0
    started_at: datetime = field(default_factory=_now)

    # 這條線特有的執行事實。預設空 dict，所以 clean 的輸出一個字元都不會變。
    # 用途是讓 lite 記下 clean 沒有的東西（原始根目錄、去重統計、產生的
    # parquet 檔名……），同時共用 run_id、時間、狀態、exit_code 的語意
    # 與寫檔位置——兩條線的執行紀錄格式一致，以後只要一套解析。
    extra: dict = field(default_factory=dict)
    # n_requests 要算哪個目錄。None = clean 的 01_request。
    dataset_dir: Path | None = None

    def stage(self, name: str) -> None:
        """記錄一個實際執行的階段。重複進入同一階段只記一次。"""
        if name not in self.stages:
            self.stages.append(name)

    def metrics(self, n_run: int, n_failed: int) -> None:
        self.n_metrics_run = int(n_run)
        self.n_metrics_failed = int(n_failed)

    def _status(self, exit_code: int, crashed: bool) -> str:
        if crashed:
            return STATUS_FAILED
        if self.n_metrics_failed:
            # 有指標壞掉但其他都跑完了：輸出可用但不完整，
            # 與「整個步驟沒跑起來」是兩件事，混在一起會讓人以為資料全毀。
            return STATUS_PARTIAL
        return STATUS_SUCCESS if exit_code == 0 else STATUS_FAILED

    def write(self, exit_code: int, crashed: bool = False) -> Path | None:
        """寫出 manifest。沒有 run_id 就沒有快照目錄，直接跳過。"""
        if not self.run_id:
            logger.debug("%s 沒有配置 run_id，不寫 %s", self.command, FILENAME)
            return None

        finished = _now()
        payload = {
            "run_id": self.run_id,
            "pipeline_version": config.PIPELINE_VERSION,
            "command": self.command,
            "status": self._status(exit_code, crashed),
            "stages": self.stages,
            "published": self.published,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_sec": round(
                (finished - self.started_at).total_seconds(), 3),
            "n_requests": _count_requests(self.dataset_dir),
            "n_metrics_run": self.n_metrics_run,
            "n_metrics_failed": self.n_metrics_failed,
            "exit_code": int(exit_code),
        }
        # 放在最後：extra 是附加事實，不該蓋掉上面任何一個共通欄位。
        for key, value in self.extra.items():
            if key in payload:
                raise ValueError(
                    f"extra 的鍵 {key!r} 與 run_manifest 的共通欄位衝突；"
                    "換一個名字，不要覆寫共通欄位。")
            payload[key] = value

        run_dir = config.RUNS_DIR / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / FILENAME
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("run_manifest：%s（%s，階段 %s）",
                    target, payload["status"], " → ".join(self.stages) or "無")
        return target
