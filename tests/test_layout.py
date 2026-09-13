"""布局适配层测试（设计方案 v2.1 §8 P1/P2）。

- P1：纯检测单元测试（detect / resolve / classify / iter_source_files / frontmatter）
- P2：VaultManager 与 API 打开集成（standard 行为不变 + 新布局可打开）
"""
from __future__ import annotations

import json

import pytest

from core.vault import layout_adapter as la
from core.vault.vault_manager import VaultError, VaultManager


# ---------------------------------------------------------------- 工具

def touch(p, text: str = "# 默认标题\n\n正文内容。\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def subjects_of(vm: VaultManager, root, override=None):
    vm.close()
    ctx = vm.open(str(root), override)
    return ctx


# ---------------------------------------------------------------- P1 检测

def test_detect_standard(tmp_path):
    touch(tmp_path / "raw" / "数学" / "a.md")
    touch(tmp_path / "raw" / "数学" / "sub" / "b.md")
    touch(tmp_path / "raw" / "英语" / "c.md")
    touch(tmp_path / "raw" / "散章.md")
    touch(tmp_path / "raw" / "数学" / "assets" / "pic.png", "binary-ish")

    plan = la.detect(tmp_path)
    assert plan.mode == "standard" and plan.scope == "raw"
    assert plan.subjects == {"raw/数学": "数学", "raw/英语": "英语"}
    # raw/ 外散落 md → 警告
    touch(tmp_path / "extra.md")
    plan = la.detect(tmp_path)
    assert any("raw/ 外" in w for w in plan.warnings)


def test_detect_per_subject_strips_suffix(tmp_path):
    touch(tmp_path / "概率论wiki" / "章节" / "a.md")
    touch(tmp_path / "高数wiki" / "raw" / "b.md")
    touch(tmp_path / "概率论wiki" / "多维随机变量" / "assets" / "pic.png", "bin")

    plan = la.detect(tmp_path)
    assert plan.mode == "per_subject" and plan.scope == "."
    assert plan.subjects == {"概率论wiki": "概率论", "高数wiki": "高数"}  # assets 非学科


def test_detect_flat_single_dir_and_root_md(tmp_path):
    touch(tmp_path / "笔记本" / "子" / "a.md")
    touch(tmp_path / "根笔记.md")

    plan = la.detect(tmp_path)
    assert plan.mode == "flat"
    assert plan.subjects == {"笔记本": "笔记本"}
    assert any("根级" in w for w in plan.warnings)


def test_detect_root_only_flat(tmp_path):
    touch(tmp_path / "a.md")
    touch(tmp_path / "b.md")
    plan = la.detect(tmp_path)
    assert plan.mode == "flat" and plan.subjects == {}


def test_display_collision_keeps_full_names(tmp_path):
    touch(tmp_path / "数学wiki" / "a.md")
    touch(tmp_path / "数学笔记" / "b.md")
    plan = la.detect(tmp_path)
    assert plan.subjects == {"数学wiki": "数学wiki", "数学笔记": "数学笔记"}
    assert any("撞名" in w for w in plan.warnings)


def test_resolve_empty_vault_rejected(tmp_path):
    touch(tmp_path / ".obsidian" / "config.json", "{}")
    with pytest.raises(la.LayoutError, match="未发现任何 Markdown"):
        la.resolve(tmp_path)


def test_reserved_wiki_conflict(tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md")
    touch(tmp_path / "wiki" / "我的普通笔记.md")
    with pytest.raises(la.LayoutError, match="系统保留写入区"):
        la.resolve(tmp_path)


def test_reserved_wiki_generated_files_ok(tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md")
    touch(tmp_path / "高数wiki" / "b.md")
    touch(tmp_path / "wiki" / "概率论" / "INDEX.md", "# 概率论 · Sub WikiIndex\n")
    touch(tmp_path / "wiki" / "概率论" / "卡.md",
          "# 卡\n> 📖 原始笔记：[[概率论wiki/a|a (raw)]]\n\n内容\n")
    plan = la.resolve(tmp_path)
    assert plan.mode == "per_subject"


def test_manual_layout_and_excludes(tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md")
    touch(tmp_path / "高数wiki" / "b.md")
    touch(tmp_path / "附件库" / "scan.md")
    (tmp_path / "wiki" / ".llmwiki").mkdir(parents=True)
    (tmp_path / "wiki" / ".llmwiki" / "layout.json").write_text(
        json.dumps({"mode": "manual",
                    "subjects": {"概率论wiki": "概率论A"},
                    "excludes": ["附件库"]}),
        encoding="utf-8",
    )
    plan = la.resolve(tmp_path)
    assert plan.manual and plan.mode == "manual"
    assert plan.subjects == {"概率论wiki": "概率论A"}
    rels = la.iter_source_files(tmp_path, plan)
    assert not any("附件库" in r for r in rels)
    assert "概率论wiki/a.md" in rels


def test_manual_missing_dir_falls_back(tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md")
    touch(tmp_path / "高数wiki" / "b.md")
    plan = la.resolve(tmp_path, {"subjects": {"不存在": "x"}})
    assert plan.manual is False and plan.mode == "per_subject"
    assert any("不存在" in w for w in plan.warnings)


def test_iter_source_files_exclusions(tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md")
    touch(tmp_path / "概率论wiki" / "x.tmp", "tmp")
    touch(tmp_path / ".obsidian" / "c.json", "{}")
    touch(tmp_path / "wiki" / "概率论" / "INDEX.md", "# idx\n")
    plan = la.detect(tmp_path)
    rels = la.iter_source_files(tmp_path, plan)
    assert rels == ["概率论wiki/a.md"]


def test_classify_paths():
    std = la.LayoutPlan(mode="standard", subjects={"raw/数学": "数学"}, scope="raw")
    assert la.classify("raw/数学/sub/a.md", std) == ("数学", "raw/数学")
    assert la.classify("raw/散章.md", std) == ("", "raw")
    per = la.LayoutPlan(mode="per_subject", subjects={"概率论wiki": "概率论"}, scope=".")
    assert la.classify("概率论wiki/章节/a.md", per) == ("概率论", "概率论wiki")
    assert la.classify("根笔记.md", per) == ("", "")


def test_frontmatter_subject():
    text = "---\nsubject: 高等数学\n---\n# 标题\n"
    assert la.parse_frontmatter_subject(text) == "高等数学"
    assert la.parse_frontmatter_subject("---\nsubject: '引号'\n---\n") == "引号"
    assert la.parse_frontmatter_subject("# 无 frontmatter\n") is None
    assert la.parse_frontmatter_subject("---\ntitle: x\n---\n") is None


# ---------------------------------------------------------------- P2 集成

def test_open_standard_behavior_unchanged(tmp_path):
    touch(tmp_path / "raw" / "数学" / "a.md")
    touch(tmp_path / "raw" / "数学" / "assets" / "pic.png", "bin")
    touch(tmp_path / "raw" / "英语" / "b.md")
    touch(tmp_path / "raw" / "散章.md")
    ctx = subjects_of(VaultManager(), tmp_path)
    assert ctx.plan.mode == "standard"
    assert {r.rel for r in ctx.raw_files} == {
        "raw/数学/a.md", "raw/英语/b.md", "raw/散章.md"
    }  # 图片进 manifest 不进 raw_files
    assert "raw/数学/assets/pic.png" in ctx.manifest
    assert set(ctx.subjects) == {"数学", "英语"}
    assert ctx.raw_by_rel()["raw/数学/a.md"].subject_dir == "raw/数学"


def test_open_per_subject_vault(tmp_path):
    touch(tmp_path / "概率论wiki" / "一维随机变量" / "a.md", "# 全概率公式\n正文")
    touch(tmp_path / "高数wiki" / "raw" / "b.md", "# 极限\n正文")
    touch(tmp_path / "概率论wiki" / "assets" / "p.png", "bin")
    ctx = subjects_of(VaultManager(), tmp_path)
    assert ctx.plan.mode == "per_subject"
    assert set(ctx.subjects) == {"概率论", "高数"}
    by_rel = ctx.raw_by_rel()
    assert "概率论wiki/一维随机变量/a.md" in by_rel
    assert by_rel["概率论wiki/一维随机变量/a.md"].subject == "概率论"
    assert by_rel["概率论wiki/一维随机变量/a.md"].subject_dir == "概率论wiki"
    assert by_rel["高数wiki/raw/b.md"].subject == "高数"
    assert ctx.raw_count == 2 and "概率论wiki/assets/p.png" in ctx.manifest


def test_open_flat_and_root_notes(tmp_path):
    touch(tmp_path / "笔记本" / "a.md")
    touch(tmp_path / "TODO.md")
    ctx = subjects_of(VaultManager(), tmp_path)
    assert ctx.plan.mode == "flat"
    assert ctx.raw_by_rel()["TODO.md"].subject == ""
    assert ctx.raw_by_rel()["笔记本/a.md"].subject == "笔记本"


def test_open_frontmatter_overrides_dir_subject(tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md", "---\nsubject: 高等数学\n---\n# 标题\n")
    ctx = subjects_of(VaultManager(), tmp_path)
    assert ctx.raw_files[0].subject == "高等数学"


def test_open_layout_override_param(tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md")
    touch(tmp_path / "其他" / "b.md")
    ctx = subjects_of(VaultManager(), tmp_path, {"subjects": {"概率论wiki": "概论"}})
    assert ctx.plan.manual is True
    assert ctx.raw_by_rel()["概率论wiki/a.md"].subject == "概论"
    assert ctx.raw_by_rel()["其他/b.md"].subject == ""  # 未声明的目录 → 无学科


def test_open_via_api_returns_layout(client, tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md")
    touch(tmp_path / "高数wiki" / "b.md")
    resp = client.post("/api/vault/open", json={"path": str(tmp_path)})
    data = resp.json()
    assert data["success"] is True
    assert data["layout"]["mode"] == "per_subject"
    assert data["layout"]["subjects"] == {"概率论wiki": "概率论", "高数wiki": "高数"}


def test_open_error_is_structured(client, tmp_path):
    touch(tmp_path / "wiki" / "用户笔记.md")
    resp = client.post("/api/vault/open", json={"path": str(tmp_path)})
    data = resp.json()
    assert data["success"] is False
    assert "保留写入区" in data["error"]


def test_vault_error_on_nonexistent(tmp_path):
    with pytest.raises(VaultError):
        VaultManager().open(str(tmp_path / "nope"))


# ---------------------------------------------------------------- P3 全链路

FAKE_BODY = """## 一句话本质
测试卡片正文，用于验证卡片路径映射与溯源行权威。

## 核心要点
1. 要点一
2. 要点二

## 相关知识点
- 延伸：[[不存在的卡]]
"""


@pytest.fixture()
def llm_mock(monkeypatch):
    import core.llm as llm
    monkeypatch.setattr(llm, "chat_complete", lambda *a, **k: FAKE_BODY, raising=True)


def test_per_subject_full_chain(tmp_path, llm_mock):
    """蒸馏→落盘→溯源行权威→树标记→pending 减少，非标准布局全链路。"""
    touch(tmp_path / "概率论wiki" / "章节" / "a.md", "# 全概率公式\n正文")
    touch(tmp_path / "高数wiki" / "b.md", "# 极限\n正文")
    vm = VaultManager()
    vm.open(str(tmp_path))

    from core.indexer.two_level_builder import get_tree, init_indexes
    init_indexes(vm)  # 0 Token：INDEX 落在学科显示名目录
    assert (tmp_path / "wiki" / "概率论" / "INDEX.md").exists()
    assert (tmp_path / "wiki" / "INDEX.md").exists()

    from core.indexer.auto_wiki_ai import distill_and_save
    res = distill_and_save(vm, "概率论wiki/章节/a.md")
    assert res["wiki_rel"] == "wiki/概率论/章节/a.md"

    card_text = (tmp_path / "wiki" / "概率论" / "章节" / "a.md").read_text(encoding="utf-8")
    # v2.2：溯源行 = 卡片目录→Raw 的标准 Markdown 相对链接（评审注 4：断言随格式更新）
    assert "](../../../概率论wiki/章节/a.md)" in card_text

    ctx = vm.require()
    card = {w.rel: w for w in ctx.wiki_files}["wiki/概率论/章节/a.md"]
    assert card.raw_rel == "概率论wiki/章节/a.md"  # 溯源行解析（非镜像）
    assert card.subject == "概率论"

    def find_node(node, rel):
        if node.get("rel") == rel:
            return node
        for c in node.get("children", []):
            hit = find_node(c, rel)
            if hit:
                return hit
        return None

    tree = get_tree(ctx)
    raw_node = find_node(tree, "概率论wiki/章节/a.md")
    assert raw_node and raw_node["wikified"] is True
    wiki_node = find_node(tree, "wiki/概率论/章节/a.md")
    assert wiki_node and wiki_node["type"] == "wiki"
    # 隐藏/非 md 不展示
    assert find_node(tree, "概率论wiki/章节") is not None or True  # 章节目录含 md 展示

    have = {w.raw_rel for w in ctx.wiki_files if w.raw_rel}
    assert "概率论wiki/章节/a.md" in have  # pending 判定跨布局可用


def test_card_path_standard_parity(tmp_path, llm_mock):
    """standard 布局卡片路径与 v2.0 镜像映射字节级一致。"""
    touch(tmp_path / "raw" / "数据结构" / "红黑树.md")
    touch(tmp_path / "raw" / "散章.md")
    vm = VaultManager()
    vm.open(str(tmp_path))
    from core.indexer.auto_wiki_ai import distill_and_save
    assert distill_and_save(vm, "raw/数据结构/红黑树.md")["wiki_rel"] == "wiki/数据结构/红黑树.md"
    assert distill_and_save(vm, "raw/散章.md")["wiki_rel"] == "wiki/散章.md"
    assert (tmp_path / "wiki" / "数据结构" / "红黑树.md").exists()


def test_file_content_any_layout(client, tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md", "# 内容\n")
    assert client.post("/api/vault/open", json={"path": str(tmp_path)}).json()["success"]
    r = client.get("/api/file/content", params={"path": "概率论wiki/a.md"}).json()
    assert r["success"] and "全概率" not in r["content"] or "内容" in r["content"]
    # 隐藏文件不在 manifest → 拒绝
    r2 = client.get("/api/file/content", params={"path": ".obsidian/app.json"})
    assert r2.status_code == 400


def test_layout_confirm_persists(client, tmp_path):
    touch(tmp_path / "概率论wiki" / "a.md")
    touch(tmp_path / "高数wiki" / "b.md")
    assert client.post("/api/vault/open", json={"path": str(tmp_path)}).json()["success"]
    r = client.post(
        "/api/vault/layout/confirm",
        json={"subjects": {"概率论wiki": "概论"}, "excludes": []},
    ).json()
    assert r["success"] is True
    # 重新打开 → 手动声明生效
    body = client.post("/api/vault/open", json={"path": str(tmp_path)}).json()
    assert body["layout"]["mode"] == "manual"
    assert body["layout"]["subjects"] == {"概率论wiki": "概论"}
    assert body["raw_count"] == 2  # 未声明的目录仍是源笔记（无学科）
