"""两级索引构建测试（Phase 3 验收：不调用 LLM 即可建立索引；Raw 内容不变）。"""
from __future__ import annotations

from core.indexer.two_level_builder import get_tree, init_indexes, parse_wikilinks
from core.vault.raw_guardian import scan_raw_manifest, verify_raw_intact
from core.vault.vault_manager import VaultManager


def test_parse_wikilinks():
    text = "关联 [[红黑树]] 与 [[raw/数据结构/AVL树.md|AVL 树 (raw)]] 以及 [[重复|别名]]"
    assert parse_wikilinks(text) == ["红黑树", "raw/数据结构/AVL树.md", "重复"]


def test_init_indexes_no_llm(demo_vault):
    hash_before = scan_raw_manifest(demo_vault)
    vm = VaultManager()
    vm.open(str(demo_vault))

    stats = init_indexes(vm)
    assert stats["index_ready"] is True
    assert stats["raw_count"] == 4
    assert stats["wiki_count"] == 0  # 0 Token：索引阶段不生成卡片

    master = (demo_vault / "wiki" / "INDEX.md").read_text(encoding="utf-8")
    assert "Master WikiIndex" in master
    assert "数据结构" in master and "计算机组成原理" in master

    sub = (demo_vault / "wiki" / "数据结构" / "INDEX.md").read_text(encoding="utf-8")
    assert "红黑树" in sub and "AVL 树" in sub
    assert "未 Wiki 化" in sub  # 尚无卡片，不产生悬空双链

    # Test 2：Raw 内容不发生改变
    assert verify_raw_intact(demo_vault, hash_before) == []


def test_index_regenerate_idempotent(demo_vault):
    vm = VaultManager()
    vm.open(str(demo_vault))
    s1 = init_indexes(vm)
    s2 = init_indexes(vm)  # 删除 wiki 索引后重跑也应一致（幂等重建）
    assert s1 == s2
    assert (demo_vault / "wiki" / "INDEX.md").exists()


def test_tree_structure(demo_vault):
    vm = VaultManager()
    vm.open(str(demo_vault))
    init_indexes(vm)
    tree = get_tree(vm.require())
    assert tree["type"] == "root"
    top = {c["name"] for c in tree["children"]}
    assert {"raw", "wiki"} <= top

    raw_node = next(c for c in tree["children"] if c["name"] == "raw")
    assert raw_node["type"] == "dir" and raw_node["count"] == 4
    ds = next(c for c in raw_node["children"] if c["name"] == "数据结构")
    assert ds["count"] == 2
    names = {f["name"] for f in ds["children"]}
    assert {"红黑树.md", "AVL树.md"} <= names
    assert all(f["type"] == "raw" and f["wikified"] is False for f in ds["children"])

    # wiki 子树：init 后只有 3 个 INDEX.md；.llmwiki/ 不展示
    wiki_node = next(c for c in tree["children"] if c["name"] == "wiki")
    assert wiki_node["count"] == 3
    wiki_files = [f for f in wiki_node["children"] if f["type"] == "wiki"]
    assert all(f["name"] == "INDEX.md" for f in wiki_files)
    assert ".llmwiki" not in {c["name"] for c in wiki_node["children"]}
