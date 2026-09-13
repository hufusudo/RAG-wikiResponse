"""RAG 单元测试：分词 / BM25+RRF / 层级路由（Phase 5 前半）。"""
from __future__ import annotations

from core.rag.hybrid_engine import HybridEngine, tokenize
from core.rag.page_table_search import PageTableSearch
from core.vault.vault_manager import VaultManager

CARD = """# 红黑树

> 📖 原始笔记：[[raw/数据结构/红黑树.md|红黑树 (raw)]]

## 一句话本质
通过红黑着色约束维持近似平衡的二叉搜索树。

## 核心要点
1. 五条性质保证最长路径不超过最短路径两倍
2. 插入删除后通过旋转与变色恢复平衡
3. 查找/插入/删除均为 O(log n)
4. 与 AVL 树相比平衡更宽松、旋转更少

## 相关知识点
- 横向对比/延伸：[[AVL树]]
"""


def _make_wiki(tmp_path):
    # raw 层（Wiki 卡片的溯源目标必须真实存在）
    d1 = tmp_path / "raw" / "数据结构"
    d1.mkdir(parents=True)
    (d1 / "红黑树.md").write_text("# 红黑树\n\n自平衡二叉搜索树。\n", encoding="utf-8")
    (d1 / "AVL树.md").write_text("# AVL 树\n\n严格平衡的 BST。\n", encoding="utf-8")

    card = tmp_path / "wiki" / "数据结构" / "红黑树.md"
    card.parent.mkdir(parents=True)
    card.write_text(CARD, encoding="utf-8")
    other = tmp_path / "wiki" / "数据结构" / "AVL树.md"
    other.write_text(
        "# AVL 树\n\n> 📖 原始笔记：[[raw/数据结构/AVL树.md|AVL 树 (raw)]]\n\n"
        "## 一句话本质\n严格平衡的二叉搜索树。\n",
        encoding="utf-8",
    )


def test_tokenize_chinese():
    toks = tokenize("红黑树 RB-Tree 保持平衡")
    assert "红黑" in toks and "黑树" in toks and "红黑树" in toks
    assert "rb" in toks and "tree" in toks


def test_hybrid_engine_ranks_exact_title_first():
    docs = [
        {"id": "b", "text": "中断机制是 CPU 响应异步事件的通道", "meta": {}},
        {"id": "a", "text": "红黑树是自平衡二叉搜索树，红黑着色保持平衡", "meta": {}},
    ]
    engine = HybridEngine(docs)
    hits = engine.search("红黑树为什么能够保持近似平衡", top_k=2)
    assert hits and hits[0]["id"] == "a"


def test_page_table_route(tmp_path):
    _make_wiki(tmp_path)
    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    search = PageTableSearch(ctx)

    res = search.retrieve("红黑树为什么能够保持近似平衡")
    levels = [s.level for s in res.trace]
    assert "sub" in levels and "wiki" in levels and "raw" in levels
    assert res.wiki_rels[0] == "wiki/数据结构/红黑树.md"  # 主命中
    assert res.raw_rels[0] == "raw/数据结构/红黑树.md"
    assert res.fallback_used is False
    # 关联卡仅作辅助摘要，其 raw 不全文读取（§7 第三级）
    assert (
        "wiki/数据结构/AVL树.md" in res.related_rels
        or len(res.wiki_rels) == 2
    )
    assert "raw/数据结构/AVL树.md" not in res.raw_rels


def test_second_hit_relevance_gate(tmp_path):
    """同学科弱相关卡不得凭 RRF 名次陪跑命中（回归：问中断溯源源到流水线）。"""
    (tmp_path / "raw" / "计算机组成原理").mkdir(parents=True)
    (tmp_path / "raw" / "计算机组成原理" / "中断机制.md").write_text(
        "# 异常与中断机制\n\n中断使 CPU 能够响应异步事件。\n", encoding="utf-8"
    )
    (tmp_path / "raw" / "计算机组成原理" / "流水线.md").write_text(
        "# 流水线\n\n指令流水线提高吞吐率。\n", encoding="utf-8"
    )
    wd = tmp_path / "wiki" / "计算机组成原理"
    wd.mkdir(parents=True)
    (wd / "中断机制.md").write_text(
        "# 异常与中断机制\n\n> 📖 原始笔记：[[raw/计算机组成原理/中断机制.md|异常与中断机制 (raw)]]\n\n"
        "## 一句话本质\nCPU 响应异步事件的机制。\n\n"
        "## 核心要点\n1. 中断源发出请求\n2. 中断控制器仲裁上报\n3. 保护现场查向量表\n4. 恢复现场返回\n5. 中断优先级与嵌套\n",
        encoding="utf-8",
    )
    (wd / "流水线.md").write_text(
        "# 流水线\n\n> 📖 原始笔记：[[raw/计算机组成原理/流水线.md|流水线 (raw)]]\n\n"
        "## 一句话本质\n指令流水线提高吞吐率。\n\n"
        "## 核心要点\n1. 五阶段\n2. 三类冒险\n3. 瓶颈级限制吞吐率\n",
        encoding="utf-8",
    )

    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    res = PageTableSearch(ctx).retrieve("什么是中断？CPU 如何响应中断？")
    assert res.wiki_rels == ["wiki/计算机组成原理/中断机制.md"]  # 弱相关卡被门槛丢弃
    assert res.raw_rels == ["raw/计算机组成原理/中断机制.md"]


def test_second_hit_kept_when_both_relevant(tmp_path):
    """两张卡都真匹配时仍应保留 Top 2（门槛不误伤）。"""
    (tmp_path / "raw" / "数据结构").mkdir(parents=True)
    (tmp_path / "raw" / "数据结构" / "红黑树.md").write_text("x", encoding="utf-8")
    (tmp_path / "raw" / "数据结构" / "AVL树.md").write_text("x", encoding="utf-8")
    wd = tmp_path / "wiki" / "数据结构"
    wd.mkdir(parents=True)
    body = "## 一句话本质\n自平衡二叉搜索树。\n\n## 核心要点\n1. 平衡\n2. 旋转\n3. O(log n)\n"
    (wd / "红黑树.md").write_text(
        "# 红黑树\n\n> 📖 原始笔记：[[raw/数据结构/红黑树.md|红黑树 (raw)]]\n\n"
        + body + "\n## 相关知识点\n- 对比：[[AVL树]]\n", encoding="utf-8"
    )
    (wd / "AVL树.md").write_text(
        "# AVL 树\n\n> 📖 原始笔记：[[raw/数据结构/AVL树.md|AVL 树 (raw)]]\n\n"
        + body, encoding="utf-8"
    )

    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    res = PageTableSearch(ctx).retrieve("红黑树和AVL树这两种平衡二叉搜索树的区别")
    assert len(res.wiki_rels) == 2  # 双真命中均保留
    assert set(res.raw_rels) == {"raw/数据结构/红黑树.md", "raw/数据结构/AVL树.md"}


def test_fallback_when_no_wiki(demo_vault):
    vm = VaultManager()
    ctx = vm.open(str(demo_vault))
    res = PageTableSearch(ctx).retrieve("红黑树的平衡原理")
    assert res.fallback_used is True
    assert res.raw_rels and "raw/数据结构/红黑树.md" in res.raw_rels


def test_all_matched_notes_fed(tmp_path):
    """门槛内全部命中都喂给 AI（无 top-2 篇数硬上限，用户约定）。"""
    (tmp_path / "raw" / "数据结构").mkdir(parents=True)
    wd = tmp_path / "wiki" / "数据结构"
    wd.mkdir(parents=True)
    body = "## 一句话本质\n经典排序算法。\n\n## 核心要点\n1. 排序思想\n2. 复杂度\n3. 稳定性\n"
    for name in ("快速排序", "归并排序", "堆排序"):
        (tmp_path / "raw" / "数据结构" / f"{name}.md").write_text("x", encoding="utf-8")
        (wd / f"{name}.md").write_text(
            f"# {name}\n\n> 📖 原始笔记：[[raw/数据结构/{name}.md|{name} (raw)]]\n\n"
            + body,
            encoding="utf-8",
        )

    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    res = PageTableSearch(ctx).retrieve("排序算法有哪些？各自的复杂度如何？")
    assert len(res.wiki_rels) == 3
    assert len(res.raw_rels) == 3
    assert {r.split("/")[-1] for r in res.raw_rels} == {
        "快速排序.md", "归并排序.md", "堆排序.md",
    }


def test_fallback_gate(tmp_path):
    """兜底路径同样受相关性门槛约束：问中断不带出流水线（回归）。"""
    (tmp_path / "raw" / "计算机组成原理").mkdir(parents=True)
    (tmp_path / "raw" / "计算机组成原理" / "中断机制.md").write_text(
        "# 异常与中断机制\n\n中断使 CPU 能够响应异步事件。\n", encoding="utf-8"
    )
    (tmp_path / "raw" / "计算机组成原理" / "流水线.md").write_text(
        "# 流水线\n\n指令流水线提高吞吐率。\n", encoding="utf-8"
    )
    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    res = PageTableSearch(ctx).retrieve("什么是中断？CPU 如何响应中断？")
    assert res.fallback_used is True
    assert res.raw_rels == ["raw/计算机组成原理/中断机制.md"]


def test_dense_only_hit_falls_back_to_raw(tmp_path):
    """回归（用户实测）：唯一 Wiki 卡与查询零词重叠时，不得遮蔽 Raw 标题兜底。

    两层根因：① 溯源行混入检索语料，其「原始笔记/原文」给任意查询白送 BM25 分；
    ② 门槛「首卡必入」放行 dense-only 陪跑卡并短路 Node 3B。
    修复后：卡片语料剥离溯源行 + 门槛要求 BM25 真命中（计划书 §1.2 语义）。
    """
    from core.graph import format_adapter as fa

    SUBJ = "向量代数与空间解析几何及多元微分学在几何上的应用"
    (tmp_path / "高数wiki" / SUBJ).mkdir(parents=True)
    (tmp_path / "高数wiki" / SUBJ / "空间平面与直线.md").write_text(
        "# 空间平面与直线\n\n平面方程与直线方程，点到平面与直线的距离。\n", encoding="utf-8"
    )
    (tmp_path / "高数wiki" / SUBJ / "方向导数与梯度.md").write_text(
        "# 方向导数与梯度\n\n方向导数是函数沿某方向的变化率，梯度是使方向导数最大的向量。\n",
        encoding="utf-8",
    )
    (tmp_path / "高数wiki" / SUBJ / "曲面与空间曲线.md").write_text(
        "# 曲面与空间曲线\n\n曲面方程。\n", encoding="utf-8"
    )
    card_rel = f"wiki/高数/{SUBJ}/空间平面与直线.md"
    raw_rel = f"高数wiki/{SUBJ}/空间平面与直线.md"
    wd = tmp_path / "wiki" / "高数" / SUBJ
    wd.mkdir(parents=True)
    (wd / "空间平面与直线.md").write_text(
        f"# 空间平面与直线\n\n{fa.build_trace_line(card_rel, raw_rel, '空间平面与直线')}\n\n"
        "## 一句话本质\n平面方程与直线方程。\n",
        encoding="utf-8",
    )

    vm = VaultManager()
    ctx = vm.open(str(tmp_path))
    search = PageTableSearch(ctx)

    # 查询主题未 Wiki 化 → dense 陪跑卡被拒 → 兜底直达 Raw 标题
    r1 = search.retrieve("我笔记中对方向导数的理解是什么？要原文")
    assert r1.wiki_rels == []
    assert r1.fallback_used is True
    assert r1.raw_rels and r1.raw_rels[0].endswith("方向导数与梯度.md")

    # 反向：真命中卡照常走主路径（门槛不误伤）
    r2 = search.retrieve("空间平面和直线方程")
    assert r2.wiki_rels and r2.wiki_rels[0].endswith("空间平面与直线.md")
    assert r2.fallback_used is False
    assert r2.raw_rels == [raw_rel]
