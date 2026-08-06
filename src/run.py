"""管線進入點。

用法::

    python -m src.run --help
    python -m src.run extract
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

logger = logging.getLogger("src.run")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_extract(args: argparse.Namespace) -> int:
    """00_raw 的 JSON → 01_request 的請求級 parquet。"""
    from src import extract

    run_id = getattr(args, "run_id", None) or config.new_run_id()
    logger.info("extract: run_id=%s，來源 %s", run_id, config.DATA_RAW)
    stats = extract.run(run_id)
    args.run_id = run_id
    return 1 if stats["failed"] and not stats["rows"] else 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    """01_request → 02_agg 的各種聚合表。"""
    logger.info("aggregate: 讀取 %s", config.DATA_REQUEST)
    print("aggregate: not implemented")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """02_agg → README 的 AUTOGEN 區塊與 runs/ 快照。"""
    logger.info("metrics: 讀取 %s", config.DATA_AGG)
    print("metrics: not implemented")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    """依序執行 extract → aggregate → metrics，任一步失敗即中止。

    三步共用同一個 run_id，快照才會落在同一個 runs/<run_id>/ 底下。
    """
    args.run_id = config.new_run_id()
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

    sub = subparsers.add_parser("aggregate", help="請求級資料表聚合成統計表")
    sub.set_defaults(func=cmd_aggregate)

    sub = subparsers.add_parser("metrics", help="產出數字並寫回 README")
    sub.set_defaults(func=cmd_metrics)

    sub = subparsers.add_parser("all", help="依序執行 extract、aggregate、metrics")
    sub.set_defaults(func=cmd_all)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    config.ensure_dirs()
    logger.debug("專案根目錄 %s", config.PROJECT_ROOT)
    logger.debug("管線版本 %s", config.PIPELINE_VERSION)

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
