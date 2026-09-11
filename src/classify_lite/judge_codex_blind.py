"""在 WSL bwrap 隔離下執行 58 群 Codex 交叉判定。

主程序只從 repo 外的盲化工作目錄組 prompt；Codex 的工作目錄也固定在
該處。模型子程序使用 deny-root 權限設定，且 shell／browser／app 等工具
均停用。所有 Codex stdout 先由父程序捕獲，不得直接流到終端。
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
BLIND_ROOT = PROJECT_ROOT.parents[1] / "codex_blind_2026-09"
OUTPUT_CSV = BLIND_ROOT / "codex_judge_output.csv"
SAMPLE_CSV = BLIND_ROOT / "blind_label_sample.csv"
CLUSTERS = BLIND_ROOT / "clusters.parquet"
PREFIXES = BLIND_ROOT / "cluster_prefixes.parquet"
GUARD_PREFIXES = SCRATCHPAD / "cluster_prefixes.parquet"
MANIFEST = BLIND_ROOT / "prompt_manifest.json"
TEMPLATE = BLIND_ROOT / "prompt_template.txt"
AXES = BLIND_ROOT / "axes_block.txt"
SCHEMA = BLIND_ROOT / "json_schema.json"
TAXONOMY = BLIND_ROOT / "taxonomy.json"
PROBE = BLIND_ROOT / "codex_context_probe.txt"
PROFILE = BLIND_ROOT / "blind.config.toml"
WRAPPER = BLIND_ROOT / "codex_exec_wrapper.sh"

WSL_DISTRO = "Ubuntu"
CODEX_VERSION = "0.153.4"
CODEX_MODEL = "gpt-5.6-sol"
CODEX_WSL = (
    "/home/isrea/.local/lib/codex-blind-0.153.4/package/vendor/"
    "x86_64-unknown-linux-musl/bin/codex"
)
WSL_PROFILE = "/home/isrea/.codex/blind.config.toml"
EXPECTED_OPUS_FINGERPRINT = (
    "e13f9738fba32a8f5167807157718758954dc2e2f1304392bdf86b3b7dd97114"
)

PROFILE_TEXT = """default_permissions = "blind"
approval_policy = "never"
web_search = "disabled"

[permissions.blind.filesystem]
":minimal" = "read"
":root" = "deny"
"/home/isrea/.local/lib/codex-blind-0.153.4" = "read"

[permissions.blind.network]
enabled = false

[features]
apps = false
browser_use = false
browser_use_external = false
computer_use = false
image_generation = false
multi_agent = false
shell_tool = false
unified_exec = false
workspace_dependencies = false
"""

OUTPUT_COLUMNS = (
    "group_id", "frame_owner", "disposition", "axis_level", "reason",
    "confidence", "model", "prompt_sha256", "judged_at", "tool_events",
)
FORBIDDEN_COLUMNS = (
    "prompt_text", "prompt", "text", "content", "message", "prefix",
    "prefix_raw", "prefix_normalized", "excerpt", "snippet", "body", "payload",
)
SAFE_ITEM_TYPES = {
    "agent_message", "reasoning", "user_message", "plan", "todo_list",
}


class CodexCallError(RuntimeError):
    def __init__(self, *, returncode: int, usage_limit: bool = False):
        super().__init__(f"Codex call failed with exit code {returncode}")
        self.returncode = returncode
        self.usage_limit = usage_limit


def windows_to_wsl(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix()[2:]
    return f"/mnt/{drive}{tail}"


def run_wsl(*args: str, input_bytes: bytes | None = None,
            timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["wsl.exe", "-d", WSL_DISTRO, "--", *args],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def install_profile() -> None:
    if PROFILE.read_text(encoding="utf-8") != PROFILE_TEXT:
        raise RuntimeError("隔離權限設定與版本控制常數不一致")
    source = windows_to_wsl(PROFILE)
    exists = run_wsl("test", "-e", WSL_PROFILE)
    if exists.returncode == 0:
        same = run_wsl("cmp", "-s", source, WSL_PROFILE)
        if same.returncode != 0:
            raise RuntimeError("WSL 已有不同內容的 blind.config.toml，不覆寫")
        return
    copied = run_wsl("cp", "--", source, WSL_PROFILE)
    if copied.returncode != 0:
        raise RuntimeError("無法安裝 WSL 盲化權限設定")


def mechanical_gate() -> bool:
    script = 'test ! -r "$1" && test ! -r "$2"'
    result = run_wsl(
        CODEX_WSL, "sandbox", "--profile", "blind",
        "--permission-profile", "blind", "--cd", windows_to_wsl(BLIND_ROOT),
        "--", "bash", "-lc", script, "blind-gate",
        windows_to_wsl(PROJECT_ROOT / "AGENTS.md"),
        windows_to_wsl(PROBE),
    )
    return result.returncode == 0


def build_prompt(template: str, axes: str, meta_fields: list[str], row,
                 prefix: str, prefix_cap: int) -> str:
    metadata = "\n".join(f"  {key}: {row[key]}" for key in meta_fields)
    return template.format(
        metadata=metadata,
        n_contents=int(row["相異內容數"]),
        prefix=prefix[:prefix_cap],
        axes=axes,
    )


def prompt_content_fingerprint(template: str, axes: str,
                               manifest: dict, schema: dict) -> str:
    payload = "\n".join([
        template,
        axes,
        ",".join(manifest["meta_fields"]),
        str(manifest["prefix_cap"]),
        manifest["prefix_field"],
        json.dumps(schema, ensure_ascii=False, sort_keys=True),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_events(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def tool_events(events: list[dict]) -> list[dict[str, object]]:
    """把 start/completed 的同一工具呼叫合併，只保留事件類型與次數。"""
    calls: dict[tuple[str, str], None] = {}
    anonymous = 0
    for event in events:
        event_type = str(event.get("type", ""))
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        item_type = str(item.get("type", "")) if item else ""
        if item_type and item_type not in SAFE_ITEM_TYPES:
            call_id = str(item.get("id") or "")
            if not call_id:
                anonymous += 1
                call_id = f"anonymous-{anonymous}"
            calls[(call_id, item_type)] = None
        elif not item and any(word in event_type.lower()
                              for word in ("tool", "command", "file_change")):
            anonymous += 1
            calls[(str(event.get("id") or f"anonymous-{anonymous}"),
                   event_type)] = None
    counts = Counter(kind for _, kind in calls)
    return [{"type": kind, "count": counts[kind]} for kind in sorted(counts)]


def final_message(events: list[dict]) -> str:
    messages = []
    for event in events:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if (event.get("type") == "item.completed"
                and item.get("type") == "agent_message"):
            messages.append(str(item.get("text", "")))
    if not messages:
        raise ValueError("Codex 沒有 agent_message")
    return messages[-1]


def call_codex(prompt: str, *, schema_path: Path | None,
               timeout: int = 300) -> tuple[str, list[dict[str, object]]]:
    if schema_path is not None and schema_path.resolve() != SCHEMA.resolve():
        raise ValueError("只允許凍結的 json schema")
    mode = "judge" if schema_path else "probe"
    result = run_wsl(
        "bash", windows_to_wsl(WRAPPER), mode,
        input_bytes=prompt.encode("utf-8"), timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.lower()
        limited = any(token in stderr for token in
                      (b"usage limit", b"rate limit", b"quota"))
        raise CodexCallError(returncode=result.returncode, usage_limit=limited)
    try:
        decoded = base64.b64decode(result.stdout, validate=False).decode("utf-8")
        events = parse_events(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexCallError(returncode=0) from exc
    return final_message(events), tool_events(events)


def reason_leaks_prefix(reason: str, prefix: str, n: int = 24) -> bool:
    if not reason or not prefix or len(reason) < n or len(prefix) < n:
        return False
    grams = {prefix[i:i + n] for i in range(len(prefix) - n + 1)}
    return any(reason[i:i + n] in grams for i in range(len(reason) - n + 1))


def parse_judgement(text: str, taxonomy: dict) -> dict[str, str]:
    value = json.loads(text)
    fields = taxonomy["FIELDS"]
    expected = set(fields) | {"reason", "confidence"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("輸出欄位不符 schema")
    for axis, allowed in fields.items():
        if value[axis] not in allowed:
            raise ValueError(f"{axis} 不在值域")
    if value["confidence"] not in {"high", "medium", "low"}:
        raise ValueError("confidence 不在值域")
    if not isinstance(value["reason"], str):
        raise ValueError("reason 不是字串")
    return {key: str(value[key]) for key in expected}


def load_existing(path: Path, fingerprint: str) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != OUTPUT_COLUMNS:
            raise RuntimeError("既有 Codex 輸出欄位不符")
        rows = list(reader)
    if len({row["group_id"] for row in rows}) != len(rows):
        raise RuntimeError("既有 Codex 輸出 group_id 重複")
    for row in rows:
        if row["prompt_sha256"] != fingerprint or row["model"] != CODEX_MODEL:
            raise RuntimeError("既有 Codex 輸出版本不符")
        parsed = json.loads(row["tool_events"])
        if not isinstance(parsed, list):
            raise RuntimeError("既有 tool_events 格式不符")
    return {row["group_id"]: row for row in rows}


def append_output(path: Path, row: dict[str, object]) -> None:
    if set(OUTPUT_COLUMNS) & set(FORBIDDEN_COLUMNS):
        raise AssertionError("輸出欄位含內容欄")
    new_file = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS,
                                extrasaction="raise")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def must_violations(rows: list[dict[str, str]], taxonomy: dict) -> list[str]:
    must = [row for row in taxonomy["AXIS_CONSTRAINTS"] if row[2] == "must"]
    failures = []
    for row in rows:
        for disposition, level, _, _ in must:
            if (row["disposition"] == disposition
                    and row["axis_level"] != level):
                failures.append(row["group_id"])
    return failures


def verify_inputs() -> dict[str, object]:
    required = (SAMPLE_CSV, CLUSTERS, PREFIXES, GUARD_PREFIXES, MANIFEST,
                TEMPLATE, AXES, SCHEMA, TAXONOMY, PROBE, PROFILE, WRAPPER)
    missing = [path.name for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"盲化工作檔缺少 {missing}")
    if any((root / name).exists() for root in [BLIND_ROOT, *BLIND_ROOT.parents]
           for name in ("AGENTS.md", "CLAUDE.md")):
        raise RuntimeError("隔離目錄祖先鏈含專案說明檔")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    taxonomy = json.loads(TAXONOMY.read_text(encoding="utf-8"))
    template = TEMPLATE.read_text(encoding="utf-8")
    axes = AXES.read_text(encoding="utf-8")
    if manifest["opus_fingerprint"] != EXPECTED_OPUS_FINGERPRINT:
        raise RuntimeError("Opus 指紋不是 e13f9738")
    actual_content_fp = prompt_content_fingerprint(
        template, axes, manifest, schema)
    if actual_content_fp != manifest["codex_prompt_content_fingerprint"]:
        raise RuntimeError("Codex 提示詞內容指紋不符")
    if manifest["codex_cli_version"] != f"codex-cli {CODEX_VERSION}":
        raise RuntimeError("manifest 的 Codex CLI 版本不符")
    if manifest["codex_model_alias"] != CODEX_MODEL:
        raise RuntimeError("manifest 的 Codex 模型不符")

    sample = pd.read_csv(SAMPLE_CSV, encoding="utf-8-sig", dtype=str,
                         usecols=["group_id"])
    group_ids = sample["group_id"].fillna("").str.strip().tolist()
    if len(group_ids) != 58 or len(set(group_ids)) != 58:
        raise RuntimeError("盲樣本不是 58 個不重複群")
    clusters = pd.read_parquet(CLUSTERS).set_index("group_id")
    prefixes = pd.read_parquet(PREFIXES).set_index("group_id")
    guard = pd.read_parquet(GUARD_PREFIXES).set_index("group_id")
    if set(group_ids) - set(clusters.index) or set(group_ids) - set(prefixes.index):
        raise RuntimeError("盲樣本與隔離 parquet 不一致")
    if set(group_ids) - set(guard.index):
        raise RuntimeError("輸出防護缺少群")
    return {
        "manifest": manifest,
        "schema": schema,
        "taxonomy": taxonomy,
        "template": template,
        "axes": axes,
        "group_ids": group_ids,
        "clusters": clusters,
        "prefixes": prefixes,
        "guard": guard,
    }


def judge_all(*, retries: int = 3) -> dict[str, object]:
    state = verify_inputs()
    install_profile()
    version = run_wsl(CODEX_WSL, "--version")
    if version.returncode != 0 or version.stdout.decode("ascii").strip() != f"codex-cli {CODEX_VERSION}":
        raise RuntimeError("WSL Codex 實際版本不符")
    if not mechanical_gate():
        raise RuntimeError("bwrap 未同時封鎖專案與隔離目錄")
    print("gate_bwrap_project=blocked")
    print("gate_bwrap_blind=blocked")

    _, probe_tools = call_codex(
        PROBE.read_text(encoding="utf-8"), schema_path=None)
    probe_tool_count = sum(int(row["count"]) for row in probe_tools)
    print(f"gate_probe_tool_events={probe_tool_count}")
    if probe_tool_count:
        raise RuntimeError("探針出現工具事件，不執行判定")

    manifest = state["manifest"]
    fingerprint = manifest["codex_execution_fingerprint"]
    existing = load_existing(OUTPUT_CSV, fingerprint)
    todo = [gid for gid in state["group_ids"] if gid not in existing]
    print(f"eligible_groups={len(state['group_ids'])}")
    print(f"already_judged={len(existing)}")
    print(f"todo_groups={len(todo)}")
    failures = []
    usage_limited = False
    for number, gid in enumerate(todo, 1):
        meta = state["clusters"].loc[gid]
        prefix = str(state["prefixes"].loc[gid, "prefix_raw"])
        prompt = build_prompt(
            state["template"], state["axes"], manifest["meta_fields"],
            meta, prefix, int(manifest["prefix_cap"]),
        )
        judged = None
        event_summary = None
        for attempt in range(1, retries + 1):
            try:
                text, event_summary = call_codex(prompt, schema_path=SCHEMA)
                judged = parse_judgement(text, state["taxonomy"])
                break
            except CodexCallError as exc:
                if exc.usage_limit:
                    usage_limited = True
                    break
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
            except (ValueError, json.JSONDecodeError):
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
        del prompt
        if usage_limited:
            print(f"progress={len(existing)}/{len(state['group_ids'])} usage_limit=true")
            break
        if judged is None or event_summary is None:
            failures.append(gid)
            print(f"progress={len(existing)}/{len(state['group_ids'])} group={gid} status=failed")
            continue
        guard_row = state["guard"].loc[gid]
        leaked = any(
            reason_leaks_prefix(judged["reason"], str(guard_row[column]))
            for column in ("prefix_raw", "prefix_normalized")
            if column in state["guard"].columns
        )
        if leaked:
            failures.append(gid)
            print(f"progress={len(existing)}/{len(state['group_ids'])} group={gid} status=reason_guard_failed")
            continue
        append_output(OUTPUT_CSV, {
            "group_id": gid,
            "frame_owner": judged["frame_owner"],
            "disposition": judged["disposition"],
            "axis_level": judged["axis_level"],
            "reason": judged["reason"],
            "confidence": judged["confidence"],
            "model": CODEX_MODEL,
            "prompt_sha256": fingerprint,
            "judged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool_events": json.dumps(event_summary, ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")),
        })
        existing[gid] = {"group_id": gid}
        event_count = sum(int(row["count"]) for row in event_summary)
        print(f"progress={len(existing)}/{len(state['group_ids'])} group={gid} status=ok tool_events={event_count}")

    rows = list(load_existing(OUTPUT_CSV, fingerprint).values())
    suspicious = []
    total_tools = 0
    for row in rows:
        summary = json.loads(row["tool_events"])
        count = sum(int(item["count"]) for item in summary)
        total_tools += count
        if count:
            suspicious.append(row["group_id"])
    violations = must_violations(rows, state["taxonomy"])
    return {
        "eligible": len(state["group_ids"]),
        "completed": len(rows),
        "failures": failures,
        "usage_limited": usage_limited,
        "probe_tool_events": probe_tool_count,
        "tool_events": total_tools,
        "suspicious_groups": suspicious,
        "must_violations": violations,
        "output": str(OUTPUT_CSV),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()
    state = verify_inputs()
    if args.dry_run:
        existing = load_existing(
            OUTPUT_CSV, state["manifest"]["codex_execution_fingerprint"])
        print(f"eligible_groups={len(state['group_ids'])}")
        print(f"already_judged={len(existing)}")
        print(f"todo_groups={len(state['group_ids']) - len(existing)}")
        print(f"opus_fingerprint={state['manifest']['opus_fingerprint']}")
        print(f"codex_prompt_content_fingerprint={state['manifest']['codex_prompt_content_fingerprint']}")
        print(f"codex_execution_fingerprint={state['manifest']['codex_execution_fingerprint']}")
        print(f"codex_model={CODEX_MODEL}")
        print(f"codex_cli=codex-cli {CODEX_VERSION}")
        return 0
    result = judge_all(retries=args.retries)
    print(f"completed_groups={result['completed']}")
    print(f"failure_count={len(result['failures'])}")
    print(f"must_violation_count={len(result['must_violations'])}")
    print(f"tool_event_total={result['tool_events']}")
    print("tool_event_groups=" + ",".join(result["suspicious_groups"]))
    return 0 if result["completed"] == result["eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
