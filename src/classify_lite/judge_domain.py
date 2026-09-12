"""逐筆 domain 判定器（本輪只準備；未經人工指示不得執行非 dry-run）。

輸入是 ``sample_domain.py`` 產生的 600 個相異內容。明文只在記憶體中讀取，
最多送出開頭 1,200 字元；stdout／stderr 由逐筆 ``OutputGuard`` 包住。
輸出不含任何文字欄位，第三人標記另以 sha256 + source_path 建安全索引。

準備檢查：``python -m src.classify_lite.judge_domain --dry-run``
判定命令刻意不寫在這裡；是否執行由下一輪明確授權決定。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from src import config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
SAMPLE_CSV = SCRATCHPAD / "domain_sample.csv"
OUTPUT_CSV = SCRATCHPAD / "judge_domain_output.csv"
THIRD_PARTY_CSV = SCRATCHPAD / "domain_named_third_party.csv"
DICTIONARY_CSV = PROJECT_ROOT / "ref" / "label_dictionary.csv"

MODEL = "claude-opus-5"
EFFORT = "medium"
CONTENT_CAP = 1200
EXPECTED_SAMPLE_SIZE = 600
BLIND_CHECK_EVERY = 25
PER_CALL_BUDGET_USD = 0.15
RUNAWAY_ABORT_USD = 60.0
CALL_TIMEOUT_S = 240
MAX_RETRIES = 4
BACKOFF_S = (5, 15, 45, 120)
SCHEMA_VERSION = "domain-v1"

DOMAIN_VALUES = (
    "clinical", "teaching", "software", "infrastructure", "research",
    "general", "unknown", "not_applicable",
)
CONFIDENCE_VALUES = ("high", "medium", "low")

OUTPUT_COLUMNS = (
    "prompt_text_sha256", "domain", "named_third_party", "confidence",
    "model", "prompt_sha256", "judged_at", "tool_events", "elapsed_sec",
    "total_cost_usd", "input_tokens", "output_tokens", "thinking_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
    "prompt_tokens_total", "schema_version",
)
THIRD_PARTY_COLUMNS = ("prompt_text_sha256", "source_path")
FORBIDDEN_OUTPUT_COLUMNS = {
    "prompt_text", "prompt", "text", "content", "message", "excerpt",
    "snippet", "body", "payload", "reason",
}

SYSTEM_PROMPT = (
    "You are a classifier. You receive one classification task per session "
    "and answer it with a single JSON object matching the given schema. "
    "You have no tools and no access to any file system, project, or prior "
    "conversation. Do not quote or reproduce the supplied content."
)

BLIND_PROBE_PROMPT = (
    "回答一個關於你目前上下文的事實問題。你的上下文裡是否有專案說明文件、"
    "先前判定結果、先前對話，或可實際呼叫的檔案系統工具？只輸出 schema "
    "要求的 JSON，不要引述任何內容。"
)
BLIND_PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": ["YES", "NO"]},
        "kind": {"type": "string"},
    },
    "required": ["answer"],
    "additionalProperties": False,
}

PROMPT_TEMPLATE = """你要判定一筆 AI Gateway 請求的內容領域。

【domain 值域】
{domain_block}

【named_third_party】
若這一筆含可指認的第三人資訊（姓名、病歷、聯絡方式、可定位的位址），填 true；
否則填 false。這個標記不改變 domain 的判定。

【內容狀態】
原始字元數：{original_length}
送入字元數：{sent_length}
是否因 1,200 字元上限截斷：{truncated}

【請求內容】
{content}

【confidence】
high：內容清楚落在一個 domain。
medium：有合理答案，但與相鄰 domain 的邊界不清楚或截斷可能有影響。
low：內容不足、語意含混，或主要依很弱的線索判斷。

只輸出一個 JSON 物件，不要有其他文字，不要引用或重述請求內容。
"""


def build_json_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "enum": list(DOMAIN_VALUES)},
            "named_third_party": {"type": "boolean"},
            "confidence": {"type": "string", "enum": list(CONFIDENCE_VALUES)},
        },
        "required": ["domain", "named_third_party", "confidence"],
        "additionalProperties": False,
    }


def load_domain_rows(path: Path = DICTIONARY_CSV) -> list[dict[str, str]]:
    rows = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    selected = rows[(rows["層"] == "分類法") & (rows["軸"] == "domain")]
    got = tuple(selected["代碼"])
    if got != DOMAIN_VALUES:
        raise ValueError(f"domain 值域順序不符：{got}")
    return selected.to_dict("records")


def render_domain(rows: list[dict[str, str]]) -> str:
    """把字典五欄逐字放進提示詞；空欄也明列，避免默默補判準。"""
    parts = []
    for row in rows:
        parts.extend([
            f"  {row['代碼']}（{row['顯示名稱']}）",
            f"    定義：{row['一句話定義'] or '（空白）'}",
            f"    判準：{row['判準'] or '（空白）'}",
            f"    處置：{row['處置'] or '（空白）'}",
            f"    具體例子：{row['具體例子'] or '（空白）'}",
            f"    邊界說明：{row['邊界說明'] or '（空白）'}",
        ])
    return "\n".join(parts)


def cli_flags(schema: dict) -> list[str]:
    """工具、專案設定與 session 持久化都在 CLI 層關閉。"""
    return [
        "-p", "--output-format", "stream-json", "--verbose",
        "--safe-mode", "--restricted", "--strict-mcp-config",
        "--disable-slash-commands", "--no-session-persistence",
        "--tools", "", "--permission-prompts", "none",
        "--model", MODEL, "--effort", EFFORT,
        "--system-prompt", SYSTEM_PROMPT,
        "--max-budget-usd", str(PER_CALL_BUDGET_USD),
        "--json-schema", json.dumps(schema, ensure_ascii=False, sort_keys=True),
    ]


def _fingerprint_flags(flags: list[str]) -> list[str]:
    """逐 token 保留實際旗標，只排除不改判定語意的逐次預算上限。"""
    kept: list[str] = []
    i = 0
    while i < len(flags):
        if flags[i] == "--max-budget-usd":
            i += 2
            continue
        kept.append(flags[i])
        i += 1
    return kept


def prompt_template(content_cap: int = CONTENT_CAP) -> str:
    """保留 1,200 版逐位元組相同，只替換校準所需的上限數字。"""
    if content_cap <= 0:
        raise ValueError("內容上限必須大於零")
    return PROMPT_TEMPLATE.replace("1,200 字元上限", f"{content_cap:,} 字元上限")


def prompt_fingerprint(domain_block: str, schema: dict,
                       content_cap: int = CONTENT_CAP) -> str:
    payload = {
        "runtime": "claude-code-stream-json",
        "model": MODEL,
        "effort": EFFORT,
        "content_cap": content_cap,
        "prompt_template": prompt_template(content_cap),
        "domain_block": domain_block,
        "system_prompt": SYSTEM_PROMPT,
        "blind_probe_prompt": BLIND_PROBE_PROMPT,
        "blind_probe_schema": BLIND_PROBE_SCHEMA,
        "cli_flags": _fingerprint_flags(cli_flags(schema)),
        "json_schema": schema,
        "output_columns": OUTPUT_COLUMNS,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_prompt(content: str, original_length: int, domain_block: str,
                 content_cap: int = CONTENT_CAP) -> str:
    sent = content[:content_cap]
    return prompt_template(content_cap).format(
        domain_block=domain_block,
        original_length=original_length,
        sent_length=len(sent),
        truncated="是" if original_length > content_cap else "否",
        content=sent,
    )


def _walk_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


def parse_stream(stdout: str) -> tuple[object, list[str], dict[str, object]]:
    """解析 stream-json，回傳結構化結果、工具事件類型、用量。"""
    events = []
    for number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"stream-json 第 {number} 行不是合法 JSON（pos {exc.pos}）")
    if not events:
        raise ValueError("claude 沒有輸出事件")

    tools: list[str] = []
    for event in events:
        for obj in _walk_objects(event):
            if obj.get("type") == "tool_use":
                tools.append(str(obj.get("name") or "unknown"))

    final = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if final is None:
        raise ValueError("stream-json 缺 result 事件")
    if final.get("is_error"):
        raise ValueError(f"claude 回報錯誤（subtype={final.get('subtype')!r}）")
    result = final.get("structured_output", final.get("result"))
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise ValueError(f"結構化輸出不是合法 JSON（pos {exc.pos}）")

    usage = final.get("usage") or {}
    cache_creation = usage.get("cache_creation_input_tokens")
    cache_read = usage.get("cache_read_input_tokens")
    input_tokens = usage.get("input_tokens")
    values = (input_tokens, cache_creation, cache_read)
    prompt_total = None
    if all(value is not None for value in values):
        prompt_total = sum(int(value) for value in values)
    envelope = {
        "total_cost_usd": final.get("total_cost_usd"),
        "input_tokens": input_tokens,
        "output_tokens": usage.get("output_tokens"),
        "thinking_tokens": usage.get("thinking_tokens"),
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "prompt_tokens_total": prompt_total,
    }
    return result, tools, envelope


def parse_judgement(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("判定結果不是 JSON 物件")
    if set(value) != {"domain", "named_third_party", "confidence"}:
        raise ValueError("判定結果欄位不符 schema")
    if value["domain"] not in DOMAIN_VALUES:
        raise ValueError("domain 不在值域")
    if type(value["named_third_party"]) is not bool:
        raise ValueError("named_third_party 不是布林值")
    if value["confidence"] not in CONFIDENCE_VALUES:
        raise ValueError("confidence 不在值域")
    return value


def overlaps(text: str, secret: str, n: int = 24) -> bool:
    """輸出字串是否與明文有 n 個連續字元重疊。"""
    if len(text) < n or len(secret) < n:
        return False
    return any(text[i:i + n] in secret for i in range(len(text) - n + 1))


class OutputGuard:
    """同時包住 stdout/stderr；逐筆把完整內容登記成禁字。"""

    def __init__(self, stream, n: int = 24):
        self._stream = stream
        self._n = n
        self._secrets: list[str] = []
        self._tail = ""

    def watch(self, *secrets: str) -> None:
        self._secrets[:] = [secret for secret in secrets if secret]

    def clear(self) -> None:
        self._secrets[:] = []

    def write(self, text):
        if text:
            probe = self._tail + text
            if any(overlaps(probe, secret, self._n) for secret in self._secrets):
                self._stream.flush()
                raise SystemExit(
                    "\nOutputGuard 阻擋可能的逐筆內容外洩；被擋字串不顯示。")
            self._tail = probe[-(self._n - 1):]
        return self._stream.write(text)

    def flush(self):
        return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_output_guard() -> OutputGuard:
    guard = OutputGuard(sys.stdout)
    error_guard = OutputGuard(sys.stderr)
    error_guard._secrets = guard._secrets
    sys.stdout = guard
    sys.stderr = error_guard
    return guard


def uninstall_output_guard() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, OutputGuard):
            setattr(sys, name, stream._stream)


def isolated_workdir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="judge_domain_"))
    for node in (path, *path.parents):
        for filename in ("AGENTS.md", "CLAUDE.md"):
            if (node / filename).exists():
                raise RuntimeError("隔離目錄的上層鏈含專案說明檔")
    return path


def run_claude(prompt: str, schema: dict, cwd: Path) -> tuple[object, list[str], dict[str, object]]:
    command = ["claude", *cli_flags(schema)]
    result = subprocess.run(
        command, input=prompt, cwd=str(cwd), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=CALL_TIMEOUT_S,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude 呼叫失敗（exit={result.returncode}）")
    return parse_stream(result.stdout)


def call_with_backoff(prompt: str, schema: dict, cwd: Path):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return run_claude(prompt, schema, cwd)
        except (subprocess.TimeoutExpired, RuntimeError):
            if attempt == MAX_RETRIES:
                raise
            time.sleep(BACKOFF_S[attempt])
    raise AssertionError("unreachable")


def blindness_check(cwd: Path) -> int:
    result, tools, _ = call_with_backoff(BLIND_PROBE_PROMPT, BLIND_PROBE_SCHEMA, cwd)
    if tools:
        raise RuntimeError("盲化探針出現工具事件")
    if not isinstance(result, dict) or result.get("answer") != "NO":
        raise RuntimeError("盲化探針未回 NO")
    return len(tools)


def load_sample(path: Path = SAMPLE_CSV) -> pd.DataFrame:
    sample = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    required = {
        "sample_order", "prompt_text_sha256", "source_path", "unit_type",
        "length_stratum", "prompt_text_len", "n_requests", "truncated_at_1200",
        "stratum_population", "stratum_sample_size", "sample_weight",
    }
    if set(sample.columns) != required:
        raise ValueError("domain_sample.csv 欄位不符")
    if len(sample) != EXPECTED_SAMPLE_SIZE:
        raise ValueError(f"domain 樣本應為 600 筆，實際 {len(sample)}")
    if sample["prompt_text_sha256"].duplicated().any():
        raise ValueError("domain 樣本有重複 sha256")
    if not sample["prompt_text_sha256"].str.fullmatch(r"[0-9a-f]{64}").all():
        raise ValueError("domain 樣本含非法 sha256")
    sample["sample_order"] = sample["sample_order"].astype(int)
    sample["prompt_text_len"] = sample["prompt_text_len"].astype(int)
    return sample.sort_values("sample_order", kind="mergesort").reset_index(drop=True)


def _safe_raw_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise ValueError("source_path 超出 lite 原始資料根目錄") from None
    return target


def load_content(row, root: Path) -> str:
    path = _safe_raw_path(root, str(row["source_path"]))
    record = json.loads(path.read_text(encoding="utf-8"))
    content = record.get("request", {}).get("prompt_text")
    if not isinstance(content, str):
        raise ValueError("原始記錄沒有文字內容")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if digest != row["prompt_text_sha256"]:
        raise ValueError("原始內容 sha256 與樣本不符")
    if len(content) != int(row["prompt_text_len"]):
        raise ValueError("原始內容長度與樣本不符")
    return content


def load_done(path: Path, fingerprint: str) -> set[str]:
    if not path.exists():
        return set()
    frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    if tuple(frame.columns) != OUTPUT_COLUMNS:
        raise ValueError("既有 domain 判定檔欄位不符")
    if set(frame["prompt_sha256"]) != {fingerprint}:
        raise ValueError("既有 domain 判定檔混有不同指紋")
    if set(frame["model"]) != {MODEL}:
        raise ValueError("既有 domain 判定檔混有不同模型")
    if frame["prompt_text_sha256"].duplicated().any():
        raise ValueError("既有 domain 判定檔有重複 sha256")
    return set(frame["prompt_text_sha256"])


def append_output(path: Path, row: dict[str, object]) -> None:
    if FORBIDDEN_OUTPUT_COLUMNS & set(row):
        raise AssertionError("domain 判定輸出含明文欄位")
    if set(row) != set(OUTPUT_COLUMNS):
        raise AssertionError("domain 判定輸出欄位集合不符")
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(OUTPUT_COLUMNS))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def rebuild_third_party_index(output_path: Path, sample: pd.DataFrame,
                              sidecar_path: Path = THIRD_PARTY_CSV) -> int:
    """從判定檔重建 true 清單；只寫 sha256 與 source_path。"""
    rows: list[dict[str, str]] = []
    if output_path.exists():
        judged = pd.read_csv(output_path, encoding="utf-8-sig", dtype=str,
                             keep_default_na=False)
        true_ids = set(judged.loc[
            judged["named_third_party"].str.lower() == "true",
            "prompt_text_sha256",
        ])
        lookup = sample.set_index("prompt_text_sha256")["source_path"]
        missing = true_ids - set(lookup.index)
        if missing:
            raise ValueError("第三人標記含不在樣本中的 sha256")
        rows = [{"prompt_text_sha256": sha, "source_path": str(lookup[sha])}
                for sha in sorted(true_ids)]
    with sidecar_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(THIRD_PARTY_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def truncation_profile(sample: pd.DataFrame,
                       content_cap: int = CONTENT_CAP) -> dict[str, object]:
    lengths = sample["prompt_text_len"].astype(int)
    truncated = lengths > content_cap
    retained = (content_cap / lengths[truncated]).clip(upper=1.0)
    return {
        "count": int(truncated.sum()),
        "share": float(truncated.mean()),
        "retained_q25": None if retained.empty else float(retained.quantile(0.25)),
        "retained_median": None if retained.empty else float(retained.quantile(0.50)),
        "retained_q75": None if retained.empty else float(retained.quantile(0.75)),
        "max_length": int(lengths.max()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="逐筆 domain 判定器")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, default=OUTPUT_CSV)
    parser.add_argument("--third-party-output", type=Path, default=THIRD_PARTY_CSV)
    args = parser.parse_args(argv)

    if FORBIDDEN_OUTPUT_COLUMNS & set(OUTPUT_COLUMNS):
        raise AssertionError("OUTPUT_COLUMNS 含明文欄位")
    sample = load_sample()
    domain_block = render_domain(load_domain_rows())
    schema = build_json_schema()
    fingerprint = prompt_fingerprint(domain_block, schema)
    done = load_done(args.output, fingerprint)
    todo = sample[~sample["prompt_text_sha256"].isin(done)].copy()
    if args.limit is not None:
        todo = todo.head(args.limit)
    profile = truncation_profile(sample)

    print("逐筆 domain 判定器（內容不輸出）")
    print(f"模型 {MODEL} effort={EFFORT}")
    print(f"提示詞指紋 {fingerprint}")
    print(f"樣本 {len(sample)}；已判 {len(done)}；本次待判 {len(todo)}")
    print(f"1,200 字元截斷 {profile['count']} 筆（{profile['share']:.1%}）")
    print("OutputGuard：stdout + stderr；逐筆完整內容為禁字")
    print("工具：--tools 空集合；每筆 tool_events 記錄事件類型")
    print("must：約束檢查 0 條（domain 沒有跨欄 must）")
    if args.dry_run:
        print("--dry-run：不讀明文、不呼叫模型，結束。")
        return 0
    if todo.empty:
        rebuild_third_party_index(args.output, sample, args.third_party_output)
        print("沒有待判內容，結束。")
        return 0

    workdir = isolated_workdir()
    root = config.lite_raw_dir()
    guard = install_output_guard()
    print(f"隔離工作目錄已建立；上層鏈無 AGENTS.md / CLAUDE.md")
    try:
        blindness_check(workdir)
    except BaseException as exc:
        guard.clear()
        uninstall_output_guard()
        raise SystemExit(f"盲化探針失敗（{type(exc).__name__}）；不執行判定") from None

    successes = failures = suspicious = 0
    spent = 0.0
    for position, (_, row) in enumerate(todo.iterrows(), 1):
        sha = str(row["prompt_text_sha256"])
        if position > 1 and (position - 1) % BLIND_CHECK_EVERY == 0:
            try:
                blindness_check(workdir)
            except BaseException as exc:
                print(f"盲化探針失敗（{type(exc).__name__}）；停止")
                break
        try:
            content = load_content(row, root)
            guard.watch(content)
            prompt = build_prompt(content, len(content), domain_block)
            started = time.monotonic()
            result, tools, usage = call_with_backoff(prompt, schema, workdir)
            elapsed = time.monotonic() - started
            judged = parse_judgement(result)
        except SystemExit:
            raise
        except Exception as exc:
            failures += 1
            print(f"[{position}/{len(todo)}] {sha[:12]}… 失敗（{type(exc).__name__}）")
            guard.clear()
            continue
        del content, prompt, result

        # 保留重複項：JSON 陣列長度就是事件數，各元素是該次事件的工具類型。
        # 去重會讓「同一工具呼叫兩次」看起來只有一個事件。
        tool_summary = sorted(tools)
        if tools:
            suspicious += 1
        cost = float(usage.get("total_cost_usd") or 0.0)
        spent += cost
        append_output(args.output, {
            "prompt_text_sha256": sha,
            "domain": judged["domain"],
            "named_third_party": str(judged["named_third_party"]).lower(),
            "confidence": judged["confidence"],
            "model": MODEL,
            "prompt_sha256": fingerprint,
            "judged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool_events": json.dumps(tool_summary, ensure_ascii=False),
            "elapsed_sec": f"{elapsed:.3f}",
            "total_cost_usd": usage.get("total_cost_usd"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "thinking_tokens": usage.get("thinking_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "prompt_tokens_total": usage.get("prompt_tokens_total"),
            "schema_version": SCHEMA_VERSION,
        })
        successes += 1
        guard.clear()
        print(f"[{position}/{len(todo)}] {sha[:12]}… 完成；tool_events={len(tools)}")
        if spent > RUNAWAY_ABORT_USD:
            print("累計估算成本超過失控偵測線；停止")
            break

    guard.clear()
    uninstall_output_guard()
    third_party = rebuild_third_party_index(args.output, sample, args.third_party_output)
    print(f"完成 {successes}；失敗 {failures}；工具事件非零 {suspicious}")
    print(f"第三人標記索引 {third_party} 筆（只含 sha256 與 source_path）")
    print(f"提示詞指紋 {fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
