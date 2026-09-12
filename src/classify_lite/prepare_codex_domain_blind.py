"""建立 repo 外的 Codex domain 盲化執行目錄；不複製樣本或內容。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.classify_lite import judge_codex_blind as codex_base
from src.classify_lite import judge_domain as opus


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BLIND_ROOT = PROJECT_ROOT.parents[1] / "codex_domain_blind_2026-09"
SCHEMA = BLIND_ROOT / "json_schema.json"
PROBE = BLIND_ROOT / "codex_context_probe.txt"
PROFILE = BLIND_ROOT / "blind.config.toml"
WRAPPER = BLIND_ROOT / "codex_exec_wrapper.sh"
MANIFEST = BLIND_ROOT / "prompt_manifest.json"
OUTPUT = BLIND_ROOT / "codex_domain_output.csv"

PROBE_TEXT = (
    "你的上下文裡有沒有專案說明文件、先前判定結果、或可讀取專案目錄的工具？"
    "請只回答這三件事各自是有或沒有，並簡短說明理由；不要讀取或顯示任何檔案內容。\n"
)


def prompt_recipe_fingerprint(domain_block: str, schema: dict) -> str:
    payload = {
        "builder": "src.classify_lite.judge_domain.build_prompt",
        "content_cap": opus.CONTENT_CAP,
        "prompt_template": opus.prompt_template(),
        "domain_block": domain_block,
        "json_schema": schema,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def wrapper_text() -> str:
    blind = codex_base.windows_to_wsl(BLIND_ROOT)
    return f"""#!/usr/bin/env bash
set -euo pipefail

CODEX={codex_base.CODEX_WSL}
BLIND={blind}

common=(
  --profile blind
  --model {codex_base.CODEX_MODEL}
  --ask-for-approval never
  --cd "$BLIND"
  exec
  --ephemeral
  --ignore-rules
  --skip-git-repo-check
  --json
)

case "${{1:-}}" in
  probe)
    "$CODEX" "${{common[@]}}" - | base64 -w0
    ;;
  judge)
    "$CODEX" "${{common[@]}}" --output-schema "$BLIND/json_schema.json" - | base64 -w0
    ;;
  *)
    exit 64
    ;;
esac
"""


def prepare() -> dict[str, object]:
    for node in (BLIND_ROOT, *BLIND_ROOT.parents):
        if any((node / name).exists() for name in ("AGENTS.md", "CLAUDE.md")):
            raise RuntimeError("Codex domain 隔離目錄祖先鏈含專案說明檔")
    BLIND_ROOT.mkdir(parents=True, exist_ok=True)
    schema = opus.build_json_schema()
    domain_block = opus.render_domain(opus.load_domain_rows())
    recipe = prompt_recipe_fingerprint(domain_block, schema)
    wrapper = wrapper_text()
    execution_payload = {
        "codex_cli": f"codex-cli {codex_base.CODEX_VERSION}",
        "codex_model": codex_base.CODEX_MODEL,
        "prompt_recipe_fingerprint": recipe,
        "profile": codex_base.PROFILE_TEXT,
        "wrapper": wrapper,
        "schema": schema,
    }
    execution_raw = json.dumps(
        execution_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"))
    execution = hashlib.sha256(execution_raw.encode("utf-8")).hexdigest()
    manifest = {
        "sample_rows": opus.EXPECTED_SAMPLE_SIZE,
        "content_cap": opus.CONTENT_CAP,
        "opus_prompt_fingerprint": opus.prompt_fingerprint(domain_block, schema),
        "codex_prompt_recipe_fingerprint": recipe,
        "codex_execution_fingerprint": execution,
        "codex_model_alias": codex_base.CODEX_MODEL,
        "codex_cli_version": f"codex-cli {codex_base.CODEX_VERSION}",
        "difference_statement": (
            "逐筆 user prompt 由 Opus 的同一 build_prompt 產生，位元組相同；"
            "差別只在模型、CLI 與各執行端固有的 host instructions。"),
        "probe_question": PROBE_TEXT.rstrip("\n"),
    }
    SCHEMA.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PROBE.write_text(PROBE_TEXT, encoding="utf-8", newline="\n")
    PROFILE.write_text(codex_base.PROFILE_TEXT, encoding="utf-8", newline="\n")
    WRAPPER.write_text(wrapper, encoding="utf-8", newline="\n")
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    manifest = prepare()
    print(f"隔離目錄：{BLIND_ROOT}")
    print("樣本或內容檔：0")
    print(f"Opus 指紋：{manifest['opus_prompt_fingerprint']}")
    print(f"Codex 提示詞配方指紋：{manifest['codex_prompt_recipe_fingerprint']}")
    print(f"Codex 執行指紋：{manifest['codex_execution_fingerprint']}")


if __name__ == "__main__":
    main()
