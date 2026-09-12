"""以 WSL bwrap 隔離執行 600 筆 Codex domain 交叉判定。"""

from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from src.classify_lite import judge_codex_blind as codex_base
from src.classify_lite import judge_domain as opus
from src.classify_lite import prepare_codex_domain_blind as prep
from src import config


OUTPUT_COLUMNS = (
    "prompt_text_sha256", "domain", "named_third_party", "confidence",
    "model", "prompt_sha256", "judged_at", "tool_events", "elapsed_sec",
    "schema_version",
)
FORBIDDEN_COLUMNS = opus.FORBIDDEN_OUTPUT_COLUMNS
MAX_RETRIES = 3
MAX_CONSECUTIVE_FAILURES = 3


def too_many_consecutive_failures(count: int) -> bool:
    """避免額度或服務失效時把所有待判項目重試一遍。"""
    return count >= MAX_CONSECUTIVE_FAILURES


def verify_inputs() -> dict[str, object]:
    required = (prep.SCHEMA, prep.PROBE, prep.PROFILE, prep.WRAPPER, prep.MANIFEST)
    missing = [path.name for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Codex domain 隔離檔缺少 {missing}")
    for node in (prep.BLIND_ROOT, *prep.BLIND_ROOT.parents):
        if any((node / name).exists() for name in ("AGENTS.md", "CLAUDE.md")):
            raise RuntimeError("Codex domain 隔離目錄祖先鏈含專案說明檔")
    manifest = json.loads(prep.MANIFEST.read_text(encoding="utf-8"))
    schema = json.loads(prep.SCHEMA.read_text(encoding="utf-8"))
    domain_block = opus.render_domain(opus.load_domain_rows())
    recipe = prep.prompt_recipe_fingerprint(domain_block, schema)
    if manifest["opus_prompt_fingerprint"] != opus.prompt_fingerprint(domain_block, schema):
        raise RuntimeError("Opus domain 指紋已改變")
    if manifest["codex_prompt_recipe_fingerprint"] != recipe:
        raise RuntimeError("Codex domain 提示詞配方已改變")
    if prep.PROFILE.read_text(encoding="utf-8") != codex_base.PROFILE_TEXT:
        raise RuntimeError("Codex domain 權限設定已改變")
    if prep.WRAPPER.read_text(encoding="utf-8") != prep.wrapper_text():
        raise RuntimeError("Codex domain wrapper 已改變")
    return {
        "manifest": manifest,
        "schema": schema,
        "domain_block": domain_block,
        "sample": opus.load_sample(),
    }


def install_profile() -> None:
    source = codex_base.windows_to_wsl(prep.PROFILE)
    exists = codex_base.run_wsl("test", "-e", codex_base.WSL_PROFILE)
    if exists.returncode == 0:
        same = codex_base.run_wsl("cmp", "-s", source, codex_base.WSL_PROFILE)
        if same.returncode != 0:
            raise RuntimeError("WSL 已有不同的 blind profile")
        return
    copied = codex_base.run_wsl("cp", "--", source, codex_base.WSL_PROFILE)
    if copied.returncode != 0:
        raise RuntimeError("無法安裝 WSL blind profile")


def mechanical_gate() -> bool:
    script = 'test ! -r "$1" && test ! -r "$2"'
    result = codex_base.run_wsl(
        codex_base.CODEX_WSL, "sandbox", "--profile", "blind",
        "--permission-profile", "blind", "--cd",
        codex_base.windows_to_wsl(prep.BLIND_ROOT), "--", "bash", "-lc",
        script, "domain-blind-gate",
        codex_base.windows_to_wsl(opus.PROJECT_ROOT / "AGENTS.md"),
        codex_base.windows_to_wsl(prep.PROBE),
    )
    return result.returncode == 0


def call_codex(prompt: str, *, use_schema: bool,
               timeout: int = 300) -> tuple[str, list[dict[str, object]]]:
    mode = "judge" if use_schema else "probe"
    result = codex_base.run_wsl(
        "bash", codex_base.windows_to_wsl(prep.WRAPPER), mode,
        input_bytes=prompt.encode("utf-8"), timeout=timeout)
    if result.returncode != 0:
        limited = any(token in result.stderr.lower() for token in
                      (b"usage limit", b"rate limit", b"quota"))
        raise codex_base.CodexCallError(
            returncode=result.returncode, usage_limit=limited)
    try:
        decoded = codex_base.base64.b64decode(
            result.stdout, validate=False).decode("utf-8")
        events = codex_base.parse_events(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise codex_base.CodexCallError(returncode=0) from exc
    return codex_base.final_message(events), codex_base.tool_events(events)


def parse_judgement(text: str) -> dict[str, object]:
    return opus.parse_judgement(json.loads(text))


def load_existing(path: Path, fingerprint: str) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != OUTPUT_COLUMNS:
            raise RuntimeError("既有 Codex domain 輸出欄位不符")
        rows = list(reader)
    if len({row["prompt_text_sha256"] for row in rows}) != len(rows):
        raise RuntimeError("既有 Codex domain 輸出 sha256 重複")
    for row in rows:
        if (row["prompt_sha256"] != fingerprint
                or row["model"] != codex_base.CODEX_MODEL):
            raise RuntimeError("既有 Codex domain 輸出版本不符")
        if not isinstance(json.loads(row["tool_events"]), list):
            raise RuntimeError("既有 Codex domain tool_events 格式不符")
    return {row["prompt_text_sha256"]: row for row in rows}


def append_output(path: Path, row: dict[str, object]) -> None:
    if set(row) & FORBIDDEN_COLUMNS or set(row) != set(OUTPUT_COLUMNS):
        raise AssertionError("Codex domain 輸出欄位含明文或集合不符")
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="raise")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def run(*, retries: int = MAX_RETRIES) -> dict[str, object]:
    state = verify_inputs()
    install_profile()
    version = codex_base.run_wsl(codex_base.CODEX_WSL, "--version")
    expected_version = f"codex-cli {codex_base.CODEX_VERSION}"
    if (version.returncode != 0
            or version.stdout.decode("ascii").strip() != expected_version):
        raise RuntimeError("Codex CLI 實際版本不符")
    if not mechanical_gate():
        raise RuntimeError("bwrap 未同時封鎖專案與隔離目錄")
    print("gate_bwrap_project=blocked")
    print("gate_bwrap_blind=blocked")
    _, probe_tools = call_codex(prep.PROBE_TEXT, use_schema=False)
    probe_count = sum(int(row["count"]) for row in probe_tools)
    print(f"gate_probe_tool_events={probe_count}")
    if probe_count:
        raise RuntimeError("Codex domain 探針有工具事件")

    fingerprint = str(state["manifest"]["codex_execution_fingerprint"])
    existing = load_existing(prep.OUTPUT, fingerprint)
    sample = state["sample"]
    todo = sample[~sample["prompt_text_sha256"].isin(existing)]
    print(f"eligible_items={len(sample)}")
    print(f"already_judged={len(existing)}")
    print(f"todo_items={len(todo)}")
    failures: list[str] = []
    usage_limited = False
    stalled = False
    consecutive_failures = 0
    root = config.lite_raw_dir()
    guard = opus.install_output_guard()
    try:
        for position, (_, row) in enumerate(todo.iterrows(), 1):
            sha = str(row["prompt_text_sha256"])
            try:
                content = opus.load_content(row, root)
                guard.watch(content)
                prompt = opus.build_prompt(
                    content, len(content), str(state["domain_block"]))
                judged = tools = None
                error_type: str | None = None
                started = time.monotonic()
                for attempt in range(1, retries + 1):
                    try:
                        text, tools = call_codex(prompt, use_schema=True)
                        judged = parse_judgement(text)
                        break
                    except codex_base.CodexCallError as exc:
                        error_type = type(exc).__name__
                        if exc.usage_limit:
                            usage_limited = True
                            break
                        if attempt < retries:
                            time.sleep(min(2 ** attempt, 8))
                    except (ValueError, json.JSONDecodeError):
                        error_type = "ValueError"
                        if attempt < retries:
                            time.sleep(min(2 ** attempt, 8))
                elapsed = time.monotonic() - started
            except SystemExit:
                raise
            except Exception as exc:
                error_type = type(exc).__name__
                failures.append(sha)
                consecutive_failures += 1
                print(f"progress={len(existing)}/{len(sample)} item={sha[:12]}… "
                      f"status=failed error={error_type}")
                guard.clear()
                if too_many_consecutive_failures(consecutive_failures):
                    stalled = True
                    print("consecutive_failures=3 stopping=true")
                    break
                continue
            del content, prompt
            guard.clear()
            if usage_limited:
                print(f"progress={len(existing)}/{len(sample)} usage_limit=true")
                break
            if judged is None or tools is None:
                failures.append(sha)
                consecutive_failures += 1
                print(f"progress={len(existing)}/{len(sample)} item={sha[:12]}… "
                      f"status=failed error={error_type or 'unknown'}")
                if too_many_consecutive_failures(consecutive_failures):
                    stalled = True
                    print("consecutive_failures=3 stopping=true")
                    break
                continue
            consecutive_failures = 0
            event_count = sum(int(item["count"]) for item in tools)
            append_output(prep.OUTPUT, {
                "prompt_text_sha256": sha,
                "domain": judged["domain"],
                "named_third_party": str(judged["named_third_party"]).lower(),
                "confidence": judged["confidence"],
                "model": codex_base.CODEX_MODEL,
                "prompt_sha256": fingerprint,
                "judged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "tool_events": json.dumps(
                    tools, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "elapsed_sec": f"{elapsed:.3f}",
                "schema_version": opus.SCHEMA_VERSION,
            })
            existing[sha] = {"prompt_text_sha256": sha}
            print(f"progress={len(existing)}/{len(sample)} item={sha[:12]}… status=ok tool_events={event_count}")
    finally:
        guard.clear()
        opus.uninstall_output_guard()

    rows = list(load_existing(prep.OUTPUT, fingerprint).values())
    total_tools = sum(
        sum(int(item["count"]) for item in json.loads(row["tool_events"]))
        for row in rows)
    return {
        "eligible": len(sample), "completed": len(rows), "failures": failures,
        "usage_limited": usage_limited, "probe_tool_events": probe_count,
        "tool_events": total_tools, "stalled": stalled,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Codex domain 盲化交叉判定")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retries", type=int, default=MAX_RETRIES)
    args = parser.parse_args(argv)
    state = verify_inputs()
    fingerprint = str(state["manifest"]["codex_execution_fingerprint"])
    existing = load_existing(prep.OUTPUT, fingerprint)
    print(f"eligible_items={len(state['sample'])}")
    print(f"already_judged={len(existing)}")
    print(f"todo_items={len(state['sample']) - len(existing)}")
    print(f"opus_prompt_fingerprint={state['manifest']['opus_prompt_fingerprint']}")
    print(f"codex_prompt_recipe_fingerprint={state['manifest']['codex_prompt_recipe_fingerprint']}")
    print(f"codex_execution_fingerprint={fingerprint}")
    print(f"codex_model={codex_base.CODEX_MODEL}")
    if args.dry_run:
        return 0
    result = run(retries=args.retries)
    print(f"completed_items={result['completed']}")
    print(f"failure_count={len(result['failures'])}")
    print(f"tool_event_total={result['tool_events']}")
    return 0 if result["completed"] == result["eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
