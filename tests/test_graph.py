"""v2.2 图模型与 DAG 安全测试（计划书 §6 验收表）。

- test_relation_extraction        提取 [text](url) 与 [[wikilink]] → 标准有向边
- test_graph_cyclic_safety        底层图 A↔B 环 → 1-hop visited 截断，无死递归
- test_standard_link_sourcing     新格式溯源行（含空格路径）解析成功率 100%
- test_graph_lint_reports_isolated_and_dangling  /api/graph/lint 孤岛与悬空报告
- test_dag_pipeline_isolation     管线单向性 + Node 5 预算均摊生效
- test_raw_hash_untouched_v22     图构建与卡片升级期间 Raw 零写操作
"""
from __future__ import annotations

import pytest

from core.graph import format_adapter as fa
from core.graph import relation_engine
from core.rag.dag_search import DagSearch, RetrievalResult, card_raw_rel
from core.vault.raw_guardian import scan_raw_manifest, verify_raw_intact
from core.vault.vault_manager import VaultManager

LEVEL_ORDER = {"master": 0, "sub": 1, "wiki": 2, "related": 3, "raw": 4}


def test_relation_extraction(tmp_path):
    """[text](url) 与 [[wikilink]] 均提取为标准有向边；frontmatter 去重；悬空记录。"""
    (tmp_path / "raw" / "数据结构").mkdir(parents=True)
    for name in ("红黑树", "AVL树", "B树"):
        (tmp_path / "raw" / "数据结构" / f"{name}.md").write_text(
            f"# {name}\n", encoding="utf-8"
        )
    wd = tmp_path / "wiki" / "数据结构"
    wd.mkdir(parents=True)
    (wd / "红黑树.md").write_text(
        "---\nrelated_to:\n  - B树\n  - 不存在的概念\n---\n"
        "# 红黑树\n\n"
        "> 📖 原始笔记：[[raw/数据结构/红黑树.md|红黑树 (raw)]]\n\n"
        "## 相关知识点\n- 标准：[AVL 树](AVL树.md)\n- 双链：[[B树]]\n",
        encoding="utf-8",
    )
    for name in ("AVL树", "B树"):
        (wd / f"{name}.md").write_text(
            f"# {name}\n\n"
            f"> 📖 原始笔记：[[raw/数据结构/{name}.md|{name} (raw)]]\n\n正文\n",
            encoding="utf-8",
        )

    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    graph = relation_engine.build(ctx)
    ok = {(e.src, e.dst, e.rtype) for e in graph.edges if e.status == "ok"}
    src = "wiki/数据结构/红黑树.md"
    assert (src, "wiki/数据结构/AVL树.md", "link") in ok      # Markdown 链接（相对卡片目录）
    assert (src, "wiki/数据结构/B树.md", "link") in ok         # [[双链]] 兼容
    # frontmatter related_to: B树 已由正文双链表达 → 去重；不存在的概念 → 悬空边
    assert (src, "wiki/数据结构/B树.md", "frontmatter") not in ok
    dangling = {e.dst for e in graph.edges if e.status == "dangling"}
    assert "不存在的概念" in dangling

    # 持久化往返：rebuild 落盘 relations.json → load 还原一致
    relation_engine.rebuild(vm)
    loaded = relation_engine.load(vm)
    assert {(e.src, e.dst, e.rtype, e.status) for e in loaded.edges} == {
        (e.src, e.dst, e.rtype, e.status) for e in graph.edges
    }


def test_graph_cyclic_safety(tmp_path):
    """底层图构造 A↔B 环：1-hop 立即停止、visited 截断、配额生效，无死递归。"""
    (tmp_path / "raw" / "操作系统").mkdir(parents=True)
    for name in ("进程", "线程", "内存"):
        (tmp_path / "raw" / "操作系统" / f"{name}.md").write_text(
            f"# {name}\n\n{name}是操作系统的核心机制。\n", encoding="utf-8"
        )
    wd = tmp_path / "wiki" / "操作系统"
    wd.mkdir(parents=True)
    (wd / "进程.md").write_text(
        "# 进程\n\n> 📖 原始笔记：[[raw/操作系统/进程.md|进程 (raw)]]\n\n"
        "进程是资源分配的基本单位。\n\n参见 [[线程]] 与 [[内存]]\n",
        encoding="utf-8",
    )
    (wd / "线程.md").write_text(
        "# 线程\n\n> 📖 原始笔记：[[raw/操作系统/线程.md|线程 (raw)]]\n\n"
        "线程是调度的基本单位。\n\n回指 [[进程]]\n",  # A↔B 环
        encoding="utf-8",
    )
    (wd / "内存.md").write_text(
        "# 内存\n\n> 📖 原始笔记：[[raw/操作系统/内存.md|内存 (raw)]]\n\n正文\n",
        encoding="utf-8",
    )

    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    graph = relation_engine.build(ctx)

    # 底层存储图允许环（真实映射网状互参）
    a = "wiki/操作系统/进程.md"
    b = "wiki/操作系统/线程.md"
    assert (a, b) in {(e.src, e.dst) for e in graph.edges}
    assert (b, a) in {(e.src, e.dst) for e in graph.edges}

    # 防环遍历（图级，确定性）：从 A 出发 1-hop 拿到 B、内存；从 B 出发拿回 A 后即停
    assert graph.related_cards([a], quota=10) == [b, "wiki/操作系统/内存.md"]
    assert graph.related_cards([b], quota=10) == [a]

    # 检索管线（BM25 首名不确定，断言不依赖排序）：无死递归、配额封顶、visited 截断
    res = DagSearch(ctx).retrieve("进程是资源分配的基本单位")
    assert res.wiki_rels  # 命中至少一张卡
    assert len(res.related_rels) <= 2
    assert len(set(res.related_rels)) == len(res.related_rels)
    assert not (set(res.related_rels) & set(res.wiki_rels))  # visited 集合截断
    core_raws = {rr for rel in res.wiki_rels if (rr := card_raw_rel(ctx, rel))}
    assert set(res.raw_rels) == core_raws  # Node 4 只溯源核心命中卡，关联卡不读 Raw
    levels = [s.level for s in res.trace]
    assert levels == sorted(levels, key=lambda lv: LEVEL_ORDER[lv])  # DAG 单向性


def test_standard_link_sourcing(tmp_path):
    """新格式溯源行 = 卡片目录→Raw 相对链接；含空格路径 <> 包裹；解析 100% 成功。"""
    raw_rel = "raw/操作系统/第三章 进程管理/线程 与 进程.md"
    (tmp_path / raw_rel).parent.mkdir(parents=True)
    (tmp_path / raw_rel).write_text("# 线程与进程\n\n两个抽象。\n", encoding="utf-8")
    card_rel = "wiki/操作系统/线程与进程.md"
    card = tmp_path / card_rel
    card.parent.mkdir(parents=True)
    card.write_text(
        f"# 线程与进程\n\n{fa.build_trace_line(card_rel, raw_rel, '线程与进程')}\n\n正文\n",
        encoding="utf-8",
    )
    # 存量旧格式卡片同样必须可解析（评审注 1：双格式兼容）
    old_rel = "wiki/操作系统/旧卡.md"
    (tmp_path / old_rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / old_rel).write_text(
        "# 旧卡\n\n> 📖 原始笔记：[[raw/操作系统/旧笔记.md|旧笔记 (raw)]]\n\n正文\n",
        encoding="utf-8",
    )
    (tmp_path / "raw" / "操作系统" / "旧笔记.md").write_text(
        "# 旧笔记\n", encoding="utf-8"
    )

    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    cards = {w.rel: w for w in ctx.wiki_files}
    assert cards[card_rel].raw_rel == raw_rel          # <> 包裹的空格路径完整还原
    assert cards[old_rel].raw_rel == "raw/操作系统/旧笔记.md"  # 旧格式
    assert card_raw_rel(ctx, card_rel) == raw_rel      # Node 4 物理溯源直达
    assert card_raw_rel(ctx, old_rel) == "raw/操作系统/旧笔记.md"


def test_layout_guard_accepts_new_trace_format(tmp_path):
    """评审注 2：守卫识别新格式溯源行，重开仓库不把自家卡片当用户文件拒绝。"""
    subj = tmp_path / "概率论wiki"
    subj.mkdir()
    (subj / "a.md").write_text("# A\n", encoding="utf-8")
    other = tmp_path / "高数wiki"
    other.mkdir()
    (other / "b.md").write_text("# B\n", encoding="utf-8")
    card = tmp_path / "wiki" / "概率论" / "卡.md"
    card.parent.mkdir(parents=True)
    card.write_text(
        "# 卡\n\n> 📖 原始笔记：[A (raw)](../../概率论wiki/a.md)\n\n正文\n",
        encoding="utf-8",
    )
    from core.vault import layout_adapter as la

    assert la.detect(tmp_path).mode == "per_subject"  # 未抛 LayoutError


def test_graph_lint_reports_isolated_and_dangling(client, tmp_path):
    """/api/graph/lint：孤岛概念（入度=0）与悬空边报告（v2.2 §2.2）。"""
    (tmp_path / "raw" / "学科").mkdir(parents=True)
    (tmp_path / "raw" / "学科" / "孤岛.md").write_text("# 孤岛\n", encoding="utf-8")
    (tmp_path / "raw" / "学科" / "源头.md").write_text("# 源头\n", encoding="utf-8")
    wd = tmp_path / "wiki" / "学科"
    wd.mkdir(parents=True)
    (wd / "孤岛.md").write_text(
        "# 孤岛\n\n> 📖 原始笔记：[[raw/学科/孤岛.md|孤岛 (raw)]]\n\n正文\n",
        encoding="utf-8",
    )
    (wd / "源头.md").write_text(
        "# 源头\n\n> 📖 原始笔记：[[raw/学科/源头.md|源头 (raw)]]\n\n"
        "引用 [[不存在的概念]] 与 [孤儿](孤儿.md)\n",
        encoding="utf-8",
    )
    assert client.post("/api/vault/open", json={"path": str(tmp_path)}).json()["success"]
    data = client.get("/api/graph/lint").json()
    assert data["success"] is True and data["nodes"] == 2
    assert "wiki/学科/孤岛.md" in data["isolated"]
    assert {"不存在的概念", "孤儿.md"} <= {d["target"] for d in data["dangling"]}


def test_dag_pipeline_isolation(tmp_path):
    """寻址管线单向不可逆；Node 5 预算均摊 PerPageLimit = max(2000, ⌊32k/N⌋) 生效。"""
    from core.generator import answer_agent

    (tmp_path / "raw" / "学科").mkdir(parents=True)
    wd = tmp_path / "wiki" / "学科"
    wd.mkdir(parents=True)
    raw_rels = []
    for name in ("甲", "乙", "丙"):
        raw_rel = f"raw/学科/{name}.md"
        (tmp_path / raw_rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / raw_rel).write_text(f"# {name}\n" + "长" * 20000, encoding="utf-8")
        raw_rels.append(raw_rel)
        (wd / f"{name}.md").write_text(
            f"# {name}\n\n> 📖 原始笔记：[[{raw_rel}|{name} (raw)]]\n\n正文\n",
            encoding="utf-8",
        )

    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    r = RetrievalResult(
        wiki_rels=["wiki/学科/甲.md"],
        related_rels=["wiki/学科/乙.md"],
        raw_rels=list(raw_rels),
    )
    context = answer_agent.build_context(ctx, "问题", r)

    n_hits = 1 + 1 + 3
    per_page = max(2000, 32000 // n_hits)
    assert per_page == 6400
    raw_segments = [s for s in context.split("#### ") if s.startswith("raw/学科/")]
    assert len(raw_segments) == 3
    for seg in raw_segments:
        body = seg.split("\n\n", 1)[0].split("\n", 1)[1]  # 去掉目标路径行，保留 Raw 正文
        assert len(body) <= per_page  # 每篇均摊截断生效（上游状态不回改）
    assert len(context) < 32000                       # 总预算不溢出
    related_seg = next(
        s for s in context.split("\n\n") if s.startswith("### Wiki：wiki/学科/乙.md")
    )
    assert len(related_seg) <= 800 + 40               # 关联卡仅 800 字摘要


@pytest.fixture()
def llm_mock(monkeypatch):
    import core.llm as llm

    monkeypatch.setattr(
        llm, "chat_complete",
        lambda *a, **k: "## 一句话本质\n测试卡片。\n\n## 核心要点\n1. 要点\n",  # noqa: E501
        raising=True,
    )


def test_obsidian_compat_trace_mode(client, tmp_path, llm_mock):
    """v2.2 §2.1：OBSIDIAN_COMPATIBILITY=true 时保留旧双链格式（可选输出适配器）。"""
    from config.settings import settings

    (tmp_path / "raw" / "数据结构").mkdir(parents=True)
    (tmp_path / "raw" / "数据结构" / "红黑树.md").write_text(
        "# 红黑树\n\n自平衡二叉搜索树。\n", encoding="utf-8"
    )
    settings.update({"OBSIDIAN_COMPATIBILITY": "true"})
    assert client.post("/api/vault/open", json={"path": str(tmp_path)}).json()["success"]
    draft = client.post(
        "/api/wiki/distill", json={"raw_path": "raw/数据结构/红黑树.md"}
    ).json()["draft"]
    assert "[[raw/数据结构/红黑树.md|红黑树 (raw)]]" in draft
    saved = client.post(
        "/api/wiki/save",
        json={"raw_path": "raw/数据结构/红黑树.md", "content": draft},
    ).json()
    assert saved["success"] is True  # 旧格式卡片仍可保存/解析（双格式兼容）


def test_raw_hash_untouched_v22(tmp_path, llm_mock):
    """图构建与卡片升级全流程中 Raw sha256 完全一致（§6 v2.2 新增）。"""
    from core.indexer import two_level_builder
    from core.indexer.auto_wiki_ai import distill_and_save

    (tmp_path / "raw" / "数据结构").mkdir(parents=True)
    (tmp_path / "raw" / "数据结构" / "红黑树.md").write_text(
        "# 红黑树\n\n自平衡二叉搜索树。\n", encoding="utf-8"
    )
    hash_before = scan_raw_manifest(tmp_path)

    vm = VaultManager()
    vm.open(str(tmp_path))
    two_level_builder.init_indexes(vm)              # 索引 + 图重建（空图）
    distill_and_save(vm, "raw/数据结构/红黑树.md")   # 卡片落盘 + 图重建 + INDEX 更新
    assert verify_raw_intact(tmp_path, hash_before) == []
