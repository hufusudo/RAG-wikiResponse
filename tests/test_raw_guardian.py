"""Raw Guard 单元测试（Phase 2 验收：可读 Raw、可写 wiki、应用 I/O 无法改 Raw）。"""
from __future__ import annotations

import pytest

from core.vault.raw_guardian import (
    RawGuardError,
    load_manifest,
    save_manifest,
    scan_raw_manifest,
    verify_raw_intact,
    write_wiki_file,
)


def test_manifest_scan(tmp_path):
    (tmp_path / "raw" / "学科").mkdir(parents=True)
    f = tmp_path / "raw" / "学科" / "红黑树.md"
    f.write_text("# 红黑树", encoding="utf-8")

    manifest = scan_raw_manifest(tmp_path)
    assert "raw/学科/红黑树.md" in manifest
    assert set(manifest["raw/学科/红黑树.md"]) >= {"mtime", "hash", "size"}
    assert save_manifest is not None


def test_verify_detects_external_change(tmp_path):
    (tmp_path / "raw").mkdir()
    f = tmp_path / "raw" / "a.md"
    f.write_text("v1", encoding="utf-8")
    manifest = scan_raw_manifest(tmp_path)
    save_manifest(tmp_path, manifest)

    assert verify_raw_intact(tmp_path, manifest) == []

    f.write_text("v2-changed", encoding="utf-8")  # 模拟外部修改
    assert verify_raw_intact(tmp_path, manifest) == ["raw/a.md"]
    # load 返回的是旧清单（记录 v1 的 hash），与磁盘不一致
    assert load_manifest(tmp_path)["raw/a.md"]["hash"] != scan_raw_manifest(tmp_path)["raw/a.md"]["hash"]


def test_write_wiki_allowed_and_raw_untouched(tmp_path):
    (tmp_path / "raw" / "学科").mkdir(parents=True)
    raw = tmp_path / "raw" / "学科" / "红黑树.md"
    raw.write_text("# 红黑树", encoding="utf-8")
    manifest = scan_raw_manifest(tmp_path)
    hash_before = manifest["raw/学科/红黑树.md"]["hash"]

    target = write_wiki_file(
        tmp_path,
        "wiki/学科/红黑树.md",
        "# 红黑树\n\n> 📖 原始笔记：[[raw/学科/红黑树.md|红黑树 (raw)]]\n",
        manifest=manifest,
        raw_rel="raw/学科/红黑树.md",
    )
    assert target.exists()
    assert raw.read_text(encoding="utf-8") == "# 红黑树"
    assert scan_raw_manifest(tmp_path)["raw/学科/红黑树.md"]["hash"] == hash_before


def test_write_to_raw_forbidden(tmp_path):
    (tmp_path / "raw").mkdir()
    with pytest.raises(RawGuardError):
        write_wiki_file(tmp_path, "raw/evil.md", "hack")


def test_path_traversal_forbidden(tmp_path):
    (tmp_path / "raw").mkdir()
    for bad in ["wiki/../evil.md", "wiki/../../outside.md", "evil.md", "wiki/x/../../y.md"]:
        with pytest.raises(RawGuardError):
            write_wiki_file(tmp_path, bad, "x")


def test_state_dir_protected(tmp_path):
    (tmp_path / "raw").mkdir()
    with pytest.raises(RawGuardError):
        write_wiki_file(tmp_path, "wiki/.llmwiki/raw_manifest.json", "x")


def test_raw_modified_before_wiki_write_fails(tmp_path):
    (tmp_path / "raw").mkdir()
    raw = tmp_path / "raw" / "a.md"
    raw.write_text("v1", encoding="utf-8")
    manifest = scan_raw_manifest(tmp_path)
    raw.write_text("v2", encoding="utf-8")  # 清单后又被改

    with pytest.raises(RawGuardError):
        write_wiki_file(tmp_path, "wiki/a.md", "x", manifest=manifest, raw_rel="raw/a.md")
    assert not (tmp_path / "wiki" / "a.md").exists()
