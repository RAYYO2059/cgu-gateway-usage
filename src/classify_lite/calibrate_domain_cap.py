"""在同一個 50 筆樣本上各跑 1,200 與 4,000 字元的 domain 校準。

輸出固定寫進獨立的 calibration 檔，不會讀寫正式 600 筆判定檔。每筆兩個
上限的先後順序用固定種子隨機化，降低時間順序與上限的混淆。不得輸出明文。
"""

from __future__ import annotations

import json
import random
import shutil
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from src import config
from src.classify_lite import judge_domain as judge


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
SAMPLE_CSV = SCRATCHPAD / "domain_cap_calibration_sample.csv"
OUTPUTS = {
    1200: SCRATCHPAD / "domain_cap_calibration.1200.csv",
    4000: SCRATCHPAD / "domain_cap_calibration.4000.csv",
}
CAPS = (1200, 4000)
CAP_ORDER_SEED = 2026091204
EXPECTED_ROWS = 50
RUNAWAY_ABORT_USD = 12.0


def load_sample(path: Path = SAMPLE_CSV) -> pd.DataFrame:
    rows = pd.read_csv(path, encoding="utf-8-sig", dtype=str,
                       keep_default_na=False)
    required = {
        "calibration_order", "sample_order", "prompt_text_sha256", "source_path",
        "unit_type", "length_stratum", "prompt_text_len", "n_requests",
        "truncated_at_1200", "arm", "arm_stratum_population",
        "arm_stratum_sample_size",
    }
    if set(rows.columns) != required:
        raise ValueError("校準樣本欄位不符")
    if len(rows) != EXPECTED_ROWS or rows["prompt_text_sha256"].duplicated().any():
        raise ValueError("校準樣本不是 50 個不重複 sha256")
    rows["calibration_order"] = rows["calibration_order"].astype(int)
    rows["prompt_text_len"] = rows["prompt_text_len"].astype(int)
    return rows.sort_values("calibration_order", kind="mergesort").reset_index(drop=True)


def cap_order(sha: str) -> tuple[int, int]:
    rng = random.Random(f"{CAP_ORDER_SEED}:{sha}")
    values = list(CAPS)
    rng.shuffle(values)
    return tuple(values)


def _load_existing(cap: int, fingerprint: str) -> set[str]:
    path = OUTPUTS[cap]
    return judge.load_done(path, fingerprint)


def _append(cap: int, sha: str, judgement: dict[str, object], tools: list[str],
            usage: dict[str, object], elapsed: float, fingerprint: str) -> None:
    judge.append_output(OUTPUTS[cap], {
        "prompt_text_sha256": sha,
        "domain": judgement["domain"],
        "named_third_party": str(judgement["named_third_party"]).lower(),
        "confidence": judgement["confidence"],
        "model": judge.MODEL,
        "prompt_sha256": fingerprint,
        "judged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool_events": json.dumps(sorted(tools), ensure_ascii=False),
        "elapsed_sec": f"{elapsed:.3f}",
        "total_cost_usd": usage.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thinking_tokens": usage.get("thinking_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "prompt_tokens_total": usage.get("prompt_tokens_total"),
        "schema_version": judge.SCHEMA_VERSION,
    })


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="domain 內容上限 50×2 校準")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    sample = load_sample()
    domain_block = judge.render_domain(judge.load_domain_rows())
    schema = judge.build_json_schema()
    fingerprints = {
        cap: judge.prompt_fingerprint(domain_block, schema, cap) for cap in CAPS}
    done = {cap: _load_existing(cap, fingerprints[cap]) for cap in CAPS}
    todo_calls = sum(
        sha not in done[cap]
        for sha in sample["prompt_text_sha256"] for cap in CAPS)
    still_truncated = {
        cap: int((sample["prompt_text_len"] > cap).sum()) for cap in CAPS}
    print("domain 上限校準（內容不輸出）")
    print(f"樣本 {len(sample)}；固定先後順序種子 {CAP_ORDER_SEED}")
    print(f"本次待判 {todo_calls}/100 次")
    for cap in CAPS:
        print(f"上限 {cap:,}：指紋 {fingerprints[cap]}；"
              f"仍截斷 {still_truncated[cap]}/{len(sample)}")
    print("正式 judge_domain_output.csv：不讀、不寫")
    if args.dry_run:
        return 0
    if not todo_calls:
        print("校準已完整，沒有待判。")
        return 0

    workdir = judge.isolated_workdir()
    root = config.lite_raw_dir()
    guard = judge.install_output_guard()
    try:
        judge.blindness_check(workdir)
    except BaseException as exc:
        guard.clear()
        judge.uninstall_output_guard()
        shutil.rmtree(workdir, ignore_errors=True)
        raise SystemExit(f"盲化探針失敗（{type(exc).__name__}）；不執行校準") from None

    successes = failures = suspicious = calls_since_probe = 0
    spent = {cap: 0.0 for cap in CAPS}
    try:
        for position, (_, row) in enumerate(sample.iterrows(), 1):
            sha = str(row["prompt_text_sha256"])
            needed = [cap for cap in cap_order(sha) if sha not in done[cap]]
            if not needed:
                continue
            try:
                content = judge.load_content(row, root)
                guard.watch(content)
            except Exception as exc:
                failures += len(needed)
                print(f"[{position}/{len(sample)}] {sha[:12]}… 讀取失敗（{type(exc).__name__}）")
                guard.clear()
                continue
            for cap in needed:
                if calls_since_probe >= judge.BLIND_CHECK_EVERY:
                    judge.blindness_check(workdir)
                    calls_since_probe = 0
                try:
                    prompt = judge.build_prompt(
                        content, len(content), domain_block, content_cap=cap)
                    started = time.monotonic()
                    result, tools, usage = judge.call_with_backoff(
                        prompt, schema, workdir)
                    elapsed = time.monotonic() - started
                    judged = judge.parse_judgement(result)
                    del prompt, result
                    _append(cap, sha, judged, tools, usage, elapsed,
                            fingerprints[cap])
                except SystemExit:
                    raise
                except Exception as exc:
                    failures += 1
                    print(f"[{position}/{len(sample)} cap={cap}] {sha[:12]}… 失敗（{type(exc).__name__}）")
                    continue
                successes += 1
                calls_since_probe += 1
                suspicious += bool(tools)
                spent[cap] += float(usage.get("total_cost_usd") or 0.0)
                done[cap].add(sha)
                print(f"[{position}/{len(sample)} cap={cap}] {sha[:12]}… 完成；tool_events={len(tools)}")
                if sum(spent.values()) > RUNAWAY_ABORT_USD:
                    print("累計估算成本超過失控偵測線；停止")
                    return 2
            del content
            guard.clear()
    finally:
        guard.clear()
        judge.uninstall_output_guard()
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"完成 {successes}；失敗 {failures}；工具事件非零 {suspicious}")
    print(f"估算成本：1,200={spent[1200]:.6f}；4,000={spent[4000]:.6f}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
