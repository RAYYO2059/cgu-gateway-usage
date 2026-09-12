"""Codex domain 交叉判定的提示詞同一性、隔離檔與明文邊界。"""

from __future__ import annotations

import json

from src.classify_lite import judge_codex_domain_blind as runner
from src.classify_lite import judge_domain as opus
from src.classify_lite import prepare_codex_domain_blind as prep


def test_codex逐筆提示詞直接使用opus同一builder():
    block = opus.render_domain(opus.load_domain_rows())
    content = "同一內容" * 400
    assert opus.build_prompt(content, len(content), block) == opus.build_prompt(
        content, len(content), block, content_cap=1200)
    assert prep.prompt_recipe_fingerprint(
        block, opus.build_json_schema()) == prep.prompt_recipe_fingerprint(
            block, opus.build_json_schema())


def test_codex輸出沒有內容或reason欄():
    assert not set(runner.OUTPUT_COLUMNS) & opus.FORBIDDEN_OUTPUT_COLUMNS
    assert "reason" not in runner.OUTPUT_COLUMNS


def test盲化目錄不複製樣本或原始內容(tmp_path, monkeypatch):
    monkeypatch.setattr(prep, "BLIND_ROOT", tmp_path / "blind")
    monkeypatch.setattr(prep, "SCHEMA", prep.BLIND_ROOT / "json_schema.json")
    monkeypatch.setattr(prep, "PROBE", prep.BLIND_ROOT / "codex_context_probe.txt")
    monkeypatch.setattr(prep, "PROFILE", prep.BLIND_ROOT / "blind.config.toml")
    monkeypatch.setattr(prep, "WRAPPER", prep.BLIND_ROOT / "codex_exec_wrapper.sh")
    monkeypatch.setattr(prep, "MANIFEST", prep.BLIND_ROOT / "prompt_manifest.json")
    monkeypatch.setattr(prep, "OUTPUT", prep.BLIND_ROOT / "codex_domain_output.csv")
    manifest = prep.prepare()
    names = {path.name for path in prep.BLIND_ROOT.iterdir()}
    assert names == {
        "json_schema.json", "codex_context_probe.txt", "blind.config.toml",
        "codex_exec_wrapper.sh", "prompt_manifest.json"}
    assert manifest["content_cap"] == 1200
    assert manifest["sample_rows"] == 600
    assert "prompt_text" not in json.dumps(manifest, ensure_ascii=False)
    assert b"\r\n" not in prep.PROFILE.read_bytes()
    assert b"\r\n" not in prep.WRAPPER.read_bytes()
