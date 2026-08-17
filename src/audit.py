"""欄位稽核：偵測原始 JSON 有、L1 沒抽的欄位（以及反過來）。

為什麼需要：extract.COLUMNS 是人工列的。上游（gateway 或 Codex 客戶端）
新增欄位時，flatten() 不會報錯也不會記錄，那個欄位就此靜默消失。
這支程式讓未映射的欄位主動現身。

兩個方向：
- 正向 unmapped_fields.csv：JSON 有、COLUMN_SOURCE_MAP 沒宣告的葉節點路徑。
- 反向 missing_sources.csv：宣告了、但資料中完全不存在的路徑。
  代表上游移除了欄位，或當初就寫了一條死路徑。

這是診斷工具，不納入 `all`。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src import config, extract

logger = logging.getLogger(__name__)

# 這些路徑（含其子路徑）一律不輸出樣本值。原因是它們裝的是使用者送出的內容
# 與本機路徑，稽核報告本身會變成外洩管道。只輸出「有幾筆有值」與長度統計。
#
# request_options.text.format.schema 是規格外追加的：第一次全量稽核跑出來，
# 發現 ...schema.properties.<欄位>.description 底下裝的是使用者自己寫的
# 中文提示語（實例："請從原文中完整擷取該專家需要閱讀的段落內容…"）。
# 那是使用者內容，只是沒放在 content.* 底下。
#
# 已知殘留風險：遮蔽只擋樣本值，擋不掉 json_path 本身——使用者自訂的
# JSON Schema 欄位名（focus_task、expert_role、rollout_slug…）仍會出現在
# 路徑欄。要根治得把這個子樹整個摺疊成一列，那會改變報告語意，留給人決定。
REDACTED_PREFIXES = (
    "content",
    "response_summary.error.message",
    "request_options.text.format.schema",
)

# 其餘欄位的樣本值，字串超過這個長度就只留長度不留內容。
SAMPLE_MAX_CHARS = 200
# 進到 CSV 的樣本值再截斷到這個長度，避免單一儲存格過長。
SAMPLE_TRUNCATE = 80
MAX_SAMPLES = 5

UNMAPPED_COLUMNS = (
    "json_path", "出現檔數", "出現比例", "值的型別", "唯一值數",
    "非空筆數", "長度_p50", "長度_max", "已遮蔽", "樣本值",
)
MISSING_COLUMNS = ("output_column", "json_path", "說明")


def is_redacted(path: str) -> bool:
    return any(
        path == prefix or path.startswith(prefix + ".")
        for prefix in REDACTED_PREFIXES
    )


@dataclass
class PathStat:
    """單一 JSON 路徑的統計。

    值本身不保留，只留雜湊——稽核跑在含個資的原始資料上，
    把值整包留在記憶體裡沒有必要，也讓這支程式更難被誤用成傾印工具。
    """

    count: int = 0
    types: Counter = field(default_factory=Counter)
    hashes: set[str] = field(default_factory=set)
    lengths: list[int] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    def observe(self, value: Any, redacted: bool) -> None:
        self.count += 1
        self.types["list" if isinstance(value, list) else type(value).__name__] += 1

        rendered = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, sort_keys=True, default=str)
        self.lengths.append(len(rendered))
        self.hashes.add(
            hashlib.blake2b(rendered.encode("utf-8", "replace"), digest_size=8).hexdigest()
        )

        if redacted or len(self.samples) >= MAX_SAMPLES:
            return
        if len(rendered) > SAMPLE_MAX_CHARS:
            # 過長的值只記長度。長字串多半是使用者內容，不該進報告。
            candidate = f"<{len(rendered)} 字元，未輸出>"
        else:
            candidate = rendered[:SAMPLE_TRUNCATE]
        if candidate not in self.samples:
            self.samples.append(candidate)


def walk(node: Any, prefix: str, leaves: dict[str, Any], containers: set[str]) -> None:
    """把巢狀結構攤成點分路徑。

    只有 dict 會展開。list 視為葉節點（值），因為 L1 對 list 的處理是
    整體序列化或計數，不是逐項展開——展開了反而對不上 COLUMN_SOURCE_MAP。
    空 dict 記成 container 而非葉節點：它「存在」但沒有值可比對。
    """
    if isinstance(node, dict):
        if prefix:
            containers.add(prefix)
        for key, value in node.items():
            walk(value, f"{prefix}.{key}" if prefix else key, leaves, containers)
        return
    if prefix:
        leaves[prefix] = node


def declared_paths() -> set[str]:
    """COLUMN_SOURCE_MAP 裡所有非衍生的來源路徑。"""
    return {p for paths in extract.COLUMN_SOURCE_MAP.values() for p in paths}


def check_map_covers_columns() -> None:
    """對照表的鍵必須與 COLUMNS 完全一致。

    不做會怎樣：新增輸出欄位卻忘了在對照表登記，該欄的來源路徑就會被
    當成「未映射」而出現在稽核報告裡——稽核工具自己製造假警報，久了沒人看。
    """
    declared = set(extract.COLUMN_SOURCE_MAP)
    produced = set(extract.COLUMNS)
    if declared != produced:
        raise ValueError(
            "COLUMN_SOURCE_MAP 與 COLUMNS 不一致：\n"
            f"  只在對照表：{sorted(declared - produced)}\n"
            f"  只在 COLUMNS：{sorted(produced - declared)}"
        )


def sample_selector(rate: float):
    """依相對路徑雜湊決定是否納入。

    用雜湊而不是 random，同一個 sample_rate 每次都選到同一批檔案，
    兩次稽核結果的差異才能歸因到資料變動，而不是抽樣抖動。
    """
    if rate >= 1.0:
        return lambda _path: True

    threshold = int(rate * (1 << 32))

    def keep(relative: str) -> bool:
        digest = hashlib.blake2b(relative.encode("utf-8"), digest_size=4).digest()
        return int.from_bytes(digest, "big") < threshold

    return keep


def scan(sample_rate: float = 1.0) -> tuple[dict[str, PathStat], set[str], int]:
    """回傳 (葉節點統計, 出現過的容器路徑, 實際掃描檔數)。"""
    keep = sample_selector(sample_rate)
    stats: dict[str, PathStat] = {}
    containers: set[str] = set()
    scanned = 0
    failed = 0

    for path in extract.scan_sources():
        relative = extract.rel_path(path)
        if not keep(relative):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            failed += 1
            logger.debug("略過無法解析的檔案 %s：%s", relative, exc)
            continue
        if not isinstance(record, dict):
            failed += 1
            continue

        scanned += 1
        leaves: dict[str, Any] = {}
        walk(record, "", leaves, containers)
        for leaf_path, value in leaves.items():
            stat = stats.get(leaf_path)
            if stat is None:
                stat = stats[leaf_path] = PathStat()
            stat.observe(value, is_redacted(leaf_path))

    if failed:
        logger.warning("稽核期間有 %d 個檔案無法解析（已略過，不影響結論方向）", failed)
    return stats, containers, scanned


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(q * (len(ordered) - 1)))
    return ordered[index]


def _row(json_path: str, stat: PathStat, scanned: int) -> list:
    redacted = is_redacted(json_path)
    return [
        json_path,
        stat.count,
        f"{stat.count / scanned:.4f}" if scanned else "",
        ",".join(f"{k}:{v}" for k, v in stat.types.most_common()),
        len(stat.hashes),
        sum(1 for n in stat.lengths if n > 0),
        _percentile(stat.lengths, 0.5),
        max(stat.lengths) if stat.lengths else 0,
        "是" if redacted else "否",
        "" if redacted else " | ".join(stat.samples),
    ]


def _write_rows(target: Path, header: tuple[str, ...], rows: list[list]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_unmapped(
    stats: dict[str, PathStat], scanned: int, target: Path
) -> list[tuple[str, PathStat]]:
    declared = declared_paths()
    unmapped = [(p, s) for p, s in stats.items() if p not in declared]
    unmapped.sort(key=lambda item: (-item[1].count, item[0]))
    _write_rows(target, UNMAPPED_COLUMNS,
                [_row(p, s, scanned) for p, s in unmapped])
    return unmapped


def write_all_fields(
    stats: dict[str, PathStat], scanned: int, target: Path
) -> int:
    """完整欄位清冊：已映射與未映射都列，多一欄標示。

    unmapped_fields.csv 依定義不會包含 content.prompt 之類「已映射」的路徑，
    但有時需要看全貌（例如確認遮蔽規則對已映射的敏感欄位也生效）。
    這份是那個用途，遮蔽規則完全相同。
    """
    declared = declared_paths()
    ordered = sorted(stats.items(), key=lambda item: (-item[1].count, item[0]))
    rows = [
        _row(p, s, scanned) + ["是" if p in declared else "否"]
        for p, s in ordered
    ]
    _write_rows(target, UNMAPPED_COLUMNS + ("已映射",), rows)
    return len(rows)


def write_missing(
    stats: dict[str, PathStat], containers: set[str], target: Path
) -> list[tuple[str, str]]:
    """宣告了但資料中不存在的來源路徑。

    「存在」包含兩種：出現在葉節點，或作為容器出現（例如
    response_summary.error 本身是 dict，永遠不會是葉節點，但它確實存在）。
    只比對葉節點的話，所有容器型 fallback 都會被誤報成已移除。

    注意這個方向對抽樣很敏感：覆蓋率 0.05% 的路徑（如 image_request.*，
    全體只有 5 個檔案有）在 sample_rate=0.1 底下幾乎必然抽不到，
    於是被誤報成「已失聯」。反向檢查只有全量掃描才可信，run() 會就此警告。
    """
    present = set(stats) | containers
    missing: list[tuple[str, str]] = []
    for column, paths in extract.COLUMN_SOURCE_MAP.items():
        for json_path in paths:
            if json_path not in present:
                missing.append((column, json_path))
    missing.sort()

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(MISSING_COLUMNS)
        for column, json_path in missing:
            writer.writerow([
                column, json_path,
                "宣告的來源路徑在掃描範圍內完全不存在：上游可能已移除，或這是死路徑",
            ])
    return missing


def run(run_id: str, sample_rate: float = 1.0,
        include_mapped: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    check_map_covers_columns()

    run_dir = config.RUNS_DIR / run_id
    logger.info("audit: 掃描 %s（sample_rate=%.3f）", config.DATA_RAW, sample_rate)

    stats, containers, scanned = scan(sample_rate)
    if not scanned:
        logger.error("audit: 沒有掃描到任何檔案")
        return {"scanned": 0, "unmapped": 0, "missing": 0}

    unmapped_file = run_dir / "unmapped_fields.csv"
    missing_file = run_dir / "missing_sources.csv"
    unmapped = write_unmapped(stats, scanned, unmapped_file)
    missing = write_missing(stats, containers, missing_file)
    all_fields_file = None
    if include_mapped:
        all_fields_file = run_dir / "all_fields.csv"
        write_all_fields(stats, scanned, all_fields_file)

    elapsed = time.perf_counter() - started
    logger.info("--- 欄位稽核報告 ---")
    logger.info("掃描檔數     %d（抽樣率 %.3f）", scanned, sample_rate)
    logger.info("葉節點路徑   %d", len(stats))
    logger.info("已宣告路徑   %d", len(declared_paths()))
    logger.info("未映射路徑   %d（明細：%s）", len(unmapped), unmapped_file)
    logger.info("失聯來源     %d（明細：%s）", len(missing), missing_file)
    if all_fields_file:
        logger.info("完整清冊     %d 條路徑（%s）", len(stats), all_fields_file)
    logger.info("耗時         %.2f 秒", elapsed)

    if unmapped:
        logger.info("出現比例最高的未映射欄位（前 10）：")
        for json_path, stat in unmapped[:10]:
            logger.info("  %-52s %6d  %5.1f%%", json_path, stat.count,
                        100 * stat.count / scanned)
    if missing:
        logger.warning("以下宣告路徑在資料中不存在：")
        for column, json_path in missing:
            logger.warning("  %s ← %s", column, json_path)
        if sample_rate < 1.0:
            logger.warning(
                "注意：sample_rate=%.3f，上述「失聯」多半是抽樣沒抽到低覆蓋率路徑，"
                "不代表上游真的移除了欄位。反向檢查請以全量掃描為準。", sample_rate,
            )

    return {
        "scanned": scanned,
        "leaf_paths": len(stats),
        "unmapped": len(unmapped),
        "missing": len(missing),
        "unmapped_file": str(unmapped_file),
        "missing_file": str(missing_file),
        "all_fields_file": str(all_fields_file) if all_fields_file else None,
        "elapsed_sec": elapsed,
    }
