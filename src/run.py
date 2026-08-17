"""管線進入點。

用法::

    python -m src.run --help
    python -m src.run extract
    python -m src.run validate
    python -m src.run aggregate
    python -m src.run metrics
    python -m src.run all
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from src import config
from src.manifest import RunManifest

logger = logging.getLogger("src.run")


def _mf(args: argparse.Namespace) -> RunManifest:
    """取得本次執行的 manifest 收集器。

    直接呼叫 cmd_* 的情境（測試、其他腳本）不會經過 main()，
    這時給一個丟棄用的收集器，讓各階段的記錄呼叫不必到處寫 if。
    """
    existing = getattr(args, "manifest", None)
    if existing is None:
        existing = RunManifest(command=getattr(args, "command", "?"))
        args.manifest = existing
    return existing


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_validate(args: argparse.Namespace) -> int:
    """對 01_request 跑 schema 驗證。不符即失敗。"""
    import pandera.pandas as pa

    from src import identity, schema

    manifest = _mf(args)
    manifest.stage("validate")
    # extract 會先設好 run_id 再呼叫本函數；單獨執行 validate 時才配置新的。
    args.run_id = getattr(args, "run_id", None) or config.new_run_id()
    manifest.run_id = args.run_id

    logger.info("validate: 讀取 %s", config.DATA_REQUEST)
    try:
        frame, warnings = schema.validate_dataset()
    except pa.errors.SchemaErrors:
        logger.error("validate: 驗證失敗，明細見上方")
        return 1
    except (FileNotFoundError, ValueError) as exc:
        logger.error("validate: %s", exc)
        return 1

    for message in warnings:
        logger.warning("validate: %s", message)

    # 身分登錄表由已驗證的資料產生，順序不可顛倒：
    # 驗證未過就代表身分欄位不可信，這時建出來的 registry 只會擴散錯誤。
    identity.run(frame)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """00_raw 的 JSON → 01_request 的請求級 parquet，完成後自動驗證。"""
    from src import extract

    manifest = _mf(args)
    manifest.stage("extract")
    run_id = getattr(args, "run_id", None) or config.new_run_id()
    manifest.run_id = run_id
    logger.info("extract: run_id=%s，來源 %s", run_id, config.DATA_RAW)
    stats = extract.run(run_id)
    args.run_id = run_id
    if stats["failed"] and not stats["rows"]:
        return 1
    # 抽取完立刻驗證：上游格式變動要在這裡就爆掉，
    # 而不是流到 aggregate 之後變成看起來很合理的錯誤數字。
    return cmd_validate(args)


def cmd_audit(args: argparse.Namespace) -> int:
    """比對原始 JSON 與 COLUMN_SOURCE_MAP，找出未映射／已失聯的欄位。"""
    from src import audit

    manifest = _mf(args)
    manifest.stage("audit")
    run_id = getattr(args, "run_id", None) or config.new_run_id()
    manifest.run_id = run_id
    if not 0 < args.sample_rate <= 1.0:
        logger.error("--sample-rate 必須落在 (0, 1]，收到 %s", args.sample_rate)
        return 1
    stats = audit.run(run_id, args.sample_rate, args.include_mapped)
    args.run_id = run_id
    return 0 if stats["scanned"] else 1


def cmd_aggregate(args: argparse.Namespace) -> int:
    """01_request → 02_agg 的 turn / thread / user 三張表。"""
    from src import aggregate

    manifest = _mf(args)
    manifest.stage("aggregate")
    run_id = getattr(args, "run_id", None) or config.new_run_id()
    manifest.run_id = run_id
    logger.info("aggregate: 讀取 %s", config.DATA_REQUEST)
    try:
        aggregate.run(run_id)
    except FileNotFoundError as exc:
        logger.error("aggregate: %s", exc)
        return 1
    args.run_id = run_id
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """執行已註冊的指標，並更新 docs/INDEX.md 與 README 的 AUTOGEN 區塊。"""
    from src import render_index
    from src.metrics import registry, runner

    if getattr(args, "list_only", False):
        specs = registry.list_metrics()
        logger.info("已註冊指標 %d 個：", len(specs))
        for spec in specs:
            logger.info(
                "  %-28s [%s/%s] v%s  %s",
                spec.name, spec.unit, spec.source, spec.version, spec.question,
            )
            if spec.group_by:
                logger.info("  %-28s   分組維度 %s（比例欄位會自動抑制）",
                            "", ", ".join(spec.group_by))
        return 0

    manifest = _mf(args)
    manifest.stage("metrics")
    run_id = getattr(args, "run_id", None) or config.new_run_id()
    manifest.run_id = run_id
    logger.info("metrics: run_id=%s，讀取 %s", run_id, config.DATA_AGG)
    try:
        summary = runner.run_all(run_id, getattr(args, "name", None))
    except (FileNotFoundError, KeyError) as exc:
        logger.error("metrics: %s", exc)
        return 1
    args.run_id = run_id
    manifest.metrics(len(summary), int((summary["狀態"] == "失敗").sum()))

    if not getattr(args, "publish", False):
        # 不帶 --publish 就完全不碰 docs/：公開版本何時更新由人決定，
        # 而不是「跑了一次指標」這個副作用。
        render_index.run()
        return 0 if (summary["狀態"] == "成功").all() else 1

    from src import render_results

    manifest.stage("publish")
    try:
        render_results.run(run_id)
    except FileNotFoundError as exc:
        logger.error("metrics --publish: %s", exc)
        return 1
    manifest.published = True
    return 0 if (summary["狀態"] == "成功").all() else 1


def cmd_all(args: argparse.Namespace) -> int:
    """依序執行 extract → aggregate → metrics，任一步失敗即中止。

    三步共用同一個 run_id，快照才會落在同一個 runs/<run_id>/ 底下。
    """
    args.run_id = config.new_run_id()
    _mf(args).run_id = args.run_id
    for step in (cmd_extract, cmd_aggregate, cmd_metrics):
        code = step(args)
        if code != 0:
            logger.error("步驟 %s 失敗，結束代碼 %d", step.__name__, code)
            return code
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.run",
        description="CGU AI Gateway 使用統計管線",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="輸出 DEBUG 等級的紀錄",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command", required=True)

    sub = subparsers.add_parser("extract", help="原始 JSON 攤平成請求級資料表")
    sub.set_defaults(func=cmd_extract)

    sub = subparsers.add_parser("validate", help="對請求級資料表跑 schema 驗證")
    sub.set_defaults(func=cmd_validate)

    # audit 刻意不納入 all：這是診斷工具，要全量重讀原始 JSON，
    # 每次跑管線都帶著它只會拖慢例行流程。
    sub = subparsers.add_parser("audit", help="稽核原始 JSON 中未被抽取的欄位")
    sub.add_argument(
        "--sample-rate",
        type=float,
        default=1.0,
        help="抽樣比例 (0, 1]，預設 1.0 全量。依檔案路徑雜湊選取，結果可重現。",
    )
    sub.add_argument(
        "--include-mapped",
        action="store_true",
        help="額外產出 all_fields.csv（已映射的路徑也列入），用於檢視完整欄位清冊",
    )
    sub.set_defaults(func=cmd_audit)

    sub = subparsers.add_parser("aggregate", help="請求級資料表聚合成統計表")
    sub.set_defaults(func=cmd_aggregate)

    sub = subparsers.add_parser("metrics", help="執行指標並更新 INDEX/README")
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--name", help="只執行這一個指標")
    group.add_argument("--list", dest="list_only", action="store_true",
                       help="列出已註冊的指標，不執行")
    sub.add_argument(
        "--publish",
        action="store_true",
        help="額外更新 docs/：複製指標 csv、重畫三張圖、渲染 RESULTS.md 與 README。"
             "不帶這個旗標就完全不碰 docs/。",
    )
    sub.set_defaults(func=cmd_metrics)

    sub = subparsers.add_parser("all", help="依序執行 extract、aggregate、metrics")
    sub.add_argument("--publish", action="store_true",
                     help="同 metrics --publish")
    sub.set_defaults(func=cmd_all)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    config.ensure_dirs()
    logger.debug("專案根目錄 %s", config.PROJECT_ROOT)
    logger.debug("管線版本 %s", config.PIPELINE_VERSION)

    # --list 只是查詢已註冊的指標，不讀資料也不產出快照，沒有東西好描述。
    if getattr(args, "list_only", False):
        return args.func(args)

    manifest = RunManifest(command=args.command)
    args.manifest = manifest
    crashed = False
    code = 1
    try:
        code = args.func(args)
    except BaseException:
        # 炸掉的執行更需要留下記錄：只在成功時才寫 manifest 的話，
        # 「哪個 run 掛了」又要退回去看檔案組成猜。
        crashed = True
        raise
    finally:
        manifest.write(code, crashed=crashed)
    return code


if __name__ == "__main__":
    sys.exit(main())
