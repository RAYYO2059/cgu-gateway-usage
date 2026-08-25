"""指標執行器。"""

from __future__ import annotations

import json
import logging

import pandas as pd

from src import config
from src.metrics import registry

logger = logging.getLogger(__name__)

SUMMARY_COLUMNS = (
    "name", "question", "unit", "source", "coverage",
    "n_total", "n_covered", "n_suppressed", "version", "狀態", "訊息",
)


def run_all(run_id: str, only: str | None = None,
            line: str = "clean") -> pd.DataFrame:
    """執行某一條線的指標並寫出 csv。回傳 summary。

    line 必須指定（預設 clean）：REGISTRY 是全域的，兩條線的指標一旦同時
    被 import 就會混在一起，而 lite 的指標拿不到 turn/thread 表會整批失敗。
    """
    if line == "lite":
        # 只在真的要跑 lite 時才 import，clean 的執行路徑不碰它。
        import src.metrics_lite  # noqa: F401
    specs = registry.list_metrics(line)
    if only:
        if only not in registry.REGISTRY:
            raise KeyError(
                f"沒有名為 {only!r} 的指標。已註冊：{sorted(registry.REGISTRY)}"
            )
        specs = [registry.REGISTRY[only]]

    tables = (registry.load_tables_lite() if line == "lite"
              else registry.load_tables())
    rules = registry.load_suppression_rules(run_id, line=line)

    out_dir = config.RUNS_DIR / run_id / ("metrics_lite" if line == "lite"
                                          else "metrics")
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for spec in specs:
        try:
            result = registry.run_metric(spec, tables, rules)
        except Exception as exc:
            # 單一指標失敗不中斷其他指標——一次跑完才知道有幾個壞掉。
            logger.exception("指標 %s 執行失敗", spec.name)
            rows.append({
                "name": spec.name, "question": spec.question, "unit": spec.unit,
                "source": spec.source, "coverage": None, "n_total": None,
                "n_covered": None, "n_suppressed": None, "version": spec.version,
                "狀態": "失敗", "訊息": f"{type(exc).__name__}: {exc}",
            })
            continue

        target = out_dir / f"{spec.name}.csv"
        result.data.to_csv(target, index=False, encoding="utf-8-sig")
        if result.suppressed:
            (out_dir / f"{spec.name}.suppressed.json").write_text(
                json.dumps(result.suppressed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        messages = list(result.warnings)
        for item in result.suppressed:
            messages.append(f"抑制 {item['維度']}={item['分組值']}（{item['原因']}）")
        rows.append({
            "name": spec.name, "question": spec.question, "unit": spec.unit,
            "source": spec.source, "coverage": round(result.coverage, 4),
            "n_total": result.n_total, "n_covered": result.n_covered,
            "n_suppressed": len(result.suppressed), "version": spec.version,
            "狀態": "成功", "訊息": " | ".join(messages),
        })
        logger.info(
            "%-28s 覆蓋率 %6.2f%%  抑制 %d 組  → %s",
            spec.name, 100 * result.coverage, len(result.suppressed), target.name,
        )
        for item in result.suppressed:
            logger.info("    抑制 %s=%s：%s", item["維度"], item["分組值"], item["原因"])
        for warning in result.warnings:
            logger.warning("    %s：%s", spec.name, warning)

    summary = pd.DataFrame(rows, columns=list(SUMMARY_COLUMNS))
    summary_path = config.RUNS_DIR / run_id / "metrics_summary.csv"
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    logger.info("指標 %d 個（成功 %d、失敗 %d）→ %s",
                len(summary), int((summary["狀態"] == "成功").sum()),
                int((summary["狀態"] == "失敗").sum()), summary_path)
    return summary
