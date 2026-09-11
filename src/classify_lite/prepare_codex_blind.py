"""建立與專案隔離的 Codex 交叉判定工作目錄。

這支程式只準備資料與提示詞快照，不啟動 Codex、不送出判定。
輸出目錄固定在 repo 外，且只包含 58 個 group_id、群層中繼資料、
截到 1,200 字元的共同前綴、值域／schema／提示詞快照與指紋清單。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRATCHPAD = PROJECT_ROOT.parent / "_rescued_scratchpad"
OUTPUT_ROOT = PROJECT_ROOT.parents[1] / "codex_blind_2026-09"
SAMPLE_CSV = SCRATCHPAD / "blind_label_sample.csv"
CLUSTERS = SCRATCHPAD / "clusters.parquet"
PREFIXES = SCRATCHPAD / "cluster_prefixes.parquet"
CARRIER = SCRATCHPAD / "review_clusters.py"
JUDGE = SCRATCHPAD / "judge_clusters.py"
OPUS_FINGERPRINT = (
    "e13f9738fba32a8f5167807157718758954dc2e2f1304392bdf86b3b7dd97114"
)
PREFIX_CAP = 1200
CODEX_MODEL_ALIAS = "gpt-5.6-sol"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法載入 {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ancestor_policy_files(root: Path) -> list[Path]:
    """回傳 root 自己與所有祖先中的政策檔；不讀取檔案內容。"""
    names = {"AGENTS.md", "CLAUDE.md"}
    return [p / name for p in [root, *root.parents] for name in names
            if (p / name).exists()]


def codex_version() -> str:
    result = subprocess.run(
        ["codex", "--version"], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build() -> dict[str, object]:
    if OUTPUT_ROOT.exists():
        raise FileExistsError(f"輸出目錄已存在，為避免覆寫而停止：{OUTPUT_ROOT}")
    if not all(path.exists() for path in (SAMPLE_CSV, CLUSTERS, PREFIXES,
                                          CARRIER, JUDGE)):
        raise FileNotFoundError("準備盲化資料所需的工作檔不完整")

    sample = pd.read_csv(
        SAMPLE_CSV, encoding="utf-8-sig", dtype=str, usecols=["group_id"])
    group_ids = sample["group_id"].fillna("").astype(str).str.strip().tolist()
    if len(group_ids) != 58 or len(set(group_ids)) != 58:
        raise ValueError(f"盲標樣本應為 58 個不重複 group_id，實際 {len(group_ids)}")

    carrier = load_module(CARRIER, "review_clusters_for_codex_blind")
    judge = load_module(JUDGE, "judge_clusters_for_codex_blind")
    if judge._load_carrier().FIELDS != carrier.FIELDS:
        raise AssertionError("判定器與載具的 FIELDS 不一致")
    if judge._load_carrier().FIELD_HELP != carrier.FIELD_HELP:
        raise AssertionError("判定器與載具的 FIELD_HELP 不一致")

    axes_block = judge.render_axes(
        carrier.FIELDS, carrier.FIELD_HELP, carrier.AXIS_CONSTRAINTS)
    schema = judge.build_json_schema(carrier.FIELDS)
    metadata_columns = ["group_id", *judge.META_FIELDS]
    clusters = pd.read_parquet(CLUSTERS, columns=metadata_columns)
    clusters = clusters.set_index("group_id").loc[group_ids].reset_index()

    prefixes = pd.read_parquet(
        PREFIXES, columns=["group_id", "prefix_raw", "truncated"])
    prefixes = prefixes.set_index("group_id").loc[group_ids].reset_index()
    prefixes["prefix_raw"] = prefixes["prefix_raw"].fillna("").astype(str)
    prefixes["truncated"] = prefixes.apply(
        lambda row: bool(row["truncated"])
        or len(row["prefix_raw"]) > PREFIX_CAP, axis=1)
    prefixes["prefix_raw"] = prefixes["prefix_raw"].str.slice(0, PREFIX_CAP)
    if int(prefixes["prefix_raw"].str.len().max()) > PREFIX_CAP:
        raise AssertionError("隔離資料的前綴超過 1,200 字元")

    OUTPUT_ROOT.mkdir(parents=True)
    sample.loc[:, ["group_id"]].to_csv(
        OUTPUT_ROOT / "blind_label_sample.csv", index=False, encoding="utf-8-sig")
    clusters.to_parquet(OUTPUT_ROOT / "clusters.parquet", index=False)
    prefixes.to_parquet(OUTPUT_ROOT / "cluster_prefixes.parquet", index=False)

    (OUTPUT_ROOT / "prompt_template.txt").write_text(
        judge.PROMPT_TEMPLATE, encoding="utf-8")
    (OUTPUT_ROOT / "axes_block.txt").write_text(axes_block, encoding="utf-8")
    write_json(OUTPUT_ROOT / "json_schema.json", schema)
    write_json(OUTPUT_ROOT / "taxonomy.json", {
        "FIELDS": {key: list(values) for key, values in carrier.FIELDS.items()},
        "FIELD_HELP": carrier.FIELD_HELP,
        "AXIS_CONSTRAINTS": [list(row) for row in carrier.AXIS_CONSTRAINTS],
    })

    prompt_payload = "\n".join([
        judge.PROMPT_TEMPLATE,
        axes_block,
        ",".join(judge.META_FIELDS),
        str(PREFIX_CAP),
        "prefix_raw",
        json.dumps(schema, ensure_ascii=False, sort_keys=True),
    ])
    prompt_content_fingerprint = hashlib.sha256(
        prompt_payload.encode("utf-8")).hexdigest()
    cli = codex_version()
    codex_execution_payload = "\n".join([
        prompt_content_fingerprint,
        "executor=codex-cli",
        f"cli={cli}",
        f"model_alias={CODEX_MODEL_ALIAS}",
    ])
    codex_fingerprint = hashlib.sha256(
        codex_execution_payload.encode("utf-8")).hexdigest()
    opus_axes = judge.render_axes(
        carrier.FIELDS, carrier.FIELD_HELP, carrier.AXIS_CONSTRAINTS)
    opus_schema = judge.build_json_schema(carrier.FIELDS)
    opus_flags = judge.cli_flags(opus_schema)
    opus_fingerprint = judge.template_fingerprint(
        opus_axes, prefix_field="raw", runtime="claude_code",
        flags=opus_flags, schema=opus_schema)
    if opus_fingerprint != OPUS_FINGERPRINT:
        raise AssertionError("現行 Opus 指紋不是 e13f9738")

    probe_question = (
        "你的上下文裡有沒有專案說明文件、先前判定結果、或可讀取專案目錄的工具？"
        "請只回答這三件事各自是有或沒有，並簡短說明理由；不要讀取或顯示任何檔案內容。"
    )
    write_json(OUTPUT_ROOT / "prompt_manifest.json", {
        "opus_fingerprint": opus_fingerprint,
        "codex_prompt_content_fingerprint": prompt_content_fingerprint,
        "codex_execution_fingerprint": codex_fingerprint,
        "difference_statement": "提示詞內容相同；差異只在執行端的 Codex 模型與 CLI。",
        "meta_fields": list(judge.META_FIELDS),
        "prefix_field": "prefix_raw",
        "prefix_cap": PREFIX_CAP,
        "codex_cli_version": cli,
        "codex_model_alias": CODEX_MODEL_ALIAS,
        "probe_question": probe_question,
        "sample_rows": len(group_ids),
        "metadata_rows": len(clusters),
        "prefix_rows": len(prefixes),
        "prefix_max_chars": int(prefixes["prefix_raw"].str.len().max()),
    })
    (OUTPUT_ROOT / "codex_context_probe.txt").write_text(
        probe_question + "\n", encoding="utf-8")

    policy_files = ancestor_policy_files(OUTPUT_ROOT)
    if policy_files:
        raise AssertionError("隔離目錄祖先鏈仍有 AGENTS.md 或 CLAUDE.md")
    actual_files = sorted(path.name for path in OUTPUT_ROOT.iterdir())
    return {
        "output_root": str(OUTPUT_ROOT),
        "sample_rows": len(group_ids),
        "metadata_rows": len(clusters),
        "prefix_rows": len(prefixes),
        "prefix_max_chars": int(prefixes["prefix_raw"].str.len().max()),
        "ancestor_policy_files": len(policy_files),
        "workspace_file_count": len(actual_files),
        "opus_fingerprint": opus_fingerprint,
        "codex_prompt_content_fingerprint": prompt_content_fingerprint,
        "codex_execution_fingerprint": codex_fingerprint,
    }


if __name__ == "__main__":
    for key, value in build().items():
        if key != "output_root":
            print(f"{key}={value}")
    print("workspace_prepared=true")
