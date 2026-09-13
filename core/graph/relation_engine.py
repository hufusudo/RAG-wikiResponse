"""概念关系引擎（计划书 v2.2 §1.1 + 评审注 4）。

存储层静态有向图（允许环，真实映射概念间网状互参）：

- **顶点**：Wiki 卡片（仓库根相对路径为唯一身份证）
- **边**：Edge(src, dst, rtype, status, weight)
- **多源关系抽取器**：
    1. Markdown 链接解析器（优先）：`[概念](相对路径)`，目标相对卡片所在目录
    2. `[[双链]]` 兼容解析器：存量卡片无需迁移
    3. 元数据抽取器：YAML frontmatter `related_to: [...]`
    4. 语义近邻抽取器（零配置可选）：Embedding 余弦 Top-1 ≥ 0.85；
       本地哈希 Embedding 噪声大（评审注 4），仅在配置 API Embedding 后开启
- **拓扑持久化**：`wiki/.llmwiki/relations.json`；卡片变更时**全量重建**
  （评审注 4：350 卡毫秒级，避免增量同步 bug），不引入外部图数据库
- **防环遍历**（§1.2 关键算法）：visited 集合 + hop 硬截断 + 配额上限三道约束
"""
from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.graph import format_adapter as fa
from core.log import log_event
from core.vault.raw_guardian import STATE_DIR
from core.vault.vault_manager import INDEX_NAME, VaultContext

SEMANTIC_MIN_COS = 0.85  # 语义近邻边阈值（§1.1）
INDEX_STEM = INDEX_NAME[:-3]  # "INDEX"


@dataclass
class Edge:
    """有向概念边。src/dst 为卡片仓库根相对路径；status=dangling 时 dst 为原始目标串。"""

    src: str
    dst: str
    rtype: str  # link | frontmatter | semantic
    status: str = "ok"  # ok | dangling
    weight: float = 1.0

    def row(self) -> list:
        return [self.src, self.dst, self.rtype, self.status, round(self.weight, 6)]


@dataclass
class RelationGraph:
    nodes: list[str] = field(default_factory=list)  # 卡片 rel（不含 INDEX）
    edges: list[Edge] = field(default_factory=list)
    built_at: str = ""
    semantic_enabled: bool = False

    # ------------------------------------------------------------ 查询

    def out(self, card_rel: str) -> list[Edge]:
        return [e for e in self.edges if e.src == card_rel]

    def in_degree(self) -> dict[str, int]:
        deg = {n: 0 for n in self.nodes}
        for e in self.edges:
            if e.status == "ok" and e.dst in deg:
                deg[e.dst] += 1
        return deg

    def related_cards(self, core_cards: list[str], quota: int = 2, hop: int = 1) -> list[str]:
        """Node 3 拓扑扩展：从命中卡出边向外漫游，三道防环约束（§1.2）。

        1. hop 硬截断（默认 1 层，O(1) 展开，绝不向下递归）
        2. Visited 集合：初始 = 核心命中卡；新节点入集合，重复目标直接丢弃
        3. 配额上限（默认 2 张）：到达即停
        """
        visited: set[str] = set(core_cards)
        found: list[str] = []
        frontier = list(core_cards)
        for _ in range(max(0, hop)):
            nxt: list[str] = []
            for card in frontier:
                for e in self.out(card):
                    if e.status != "ok" or e.dst in visited:
                        continue
                    visited.add(e.dst)
                    found.append(e.dst)
                    if len(found) >= quota:
                        return found
                    nxt.append(e.dst)
            frontier = nxt
        return found

    def lint(self) -> dict:
        """知识库巡检（v2.2 §2.2）：孤岛概念（入度=0）与悬空边报告。"""
        deg = self.in_degree()
        dangling = [
            {"src": e.src, "target": e.dst, "rtype": e.rtype}
            for e in self.edges
            if e.status == "dangling"
        ]
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "semantic_enabled": self.semantic_enabled,
            "isolated": sorted(n for n, d in deg.items() if d == 0),
            "dangling": dangling,
            "built_at": self.built_at,
        }

    # ------------------------------------------------------------ 序列化

    def to_json(self) -> dict:
        return {
            "version": 1,
            "built_at": self.built_at,
            "semantic_enabled": self.semantic_enabled,
            "nodes": self.nodes,
            "edges": [e.row() for e in self.edges],
        }

    @classmethod
    def from_json(cls, data: dict) -> "RelationGraph":
        edges = [Edge(*row) for row in data.get("edges", [])]
        return cls(
            nodes=list(data.get("nodes", [])),
            edges=edges,
            built_at=data.get("built_at", ""),
            semantic_enabled=bool(data.get("semantic_enabled", False)),
        )


# ---------------------------------------------------------------- 持久化位置

def relations_file(root: Path) -> Path:
    return root / "wiki" / STATE_DIR / "relations.json"


# ---------------------------------------------------------------- 抽取

_FM_RELATED_RE = re.compile(r"^related_to:\s*(.*)$")


def parse_frontmatter_related(text: str) -> list[str]:
    """frontmatter `related_to` 键（行内 `[a, b]` 或 `- a` 列表，前 40 行内）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    out: list[str] = []
    in_block = False
    for line in lines[1:41]:
        s = line.strip()
        if s == "---":
            break
        m = _FM_RELATED_RE.match(s)
        if m:
            in_block = True
            inline = m.group(1).strip()
            if inline:  # 行内写法：related_to: [A, B]
                inner = inline.strip("[]")
                out += [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
            continue
        if in_block:
            if s.startswith("- "):
                out.append(s[2:].strip().strip("'\""))
            elif s:  # 下一个键 → 列表结束
                in_block = False
    return [x for x in out if x]


def _card_lookup(ctx: VaultContext) -> tuple[list, dict[str, str], set[str]]:
    """(卡片列表, stem→rel 索引, 全部卡片 rel 集合)。INDEX 与 .llmwiki 元数据不在图中。"""
    cards = [w for w in ctx.wiki_files if not w.is_index]
    by_stem: dict[str, str] = {}
    for w in cards:
        by_stem.setdefault(w.name, w.rel)
    return cards, by_stem, {w.rel for w in cards}


def resolve_target(
    target: str, card_rel: str, by_stem: dict[str, str], card_rels: set[str]
) -> str | None:
    """链接目标 → 卡片 rel；无法解析为已有卡片返回 None（→ 悬空候选）。

    兼容三种书写：`[[名称]]`、`[文本](相对路径.md)`、`[文本](wiki/全路径.md)`。
    学科子索引（…/INDEX）不作为概念卡片（与 v2.1 行为一致）。
    """
    t = target.strip()
    if not t:
        return None
    stem = t.rsplit("/", 1)[-1].strip()
    if stem == INDEX_STEM and "/" in t:
        return None
    for c in (t, fa.resolve_relpath(card_rel, t)):
        for v in (c, c[:-3] if c.endswith(".md") else None):
            if v and v in card_rels:
                return v
    if stem in by_stem:
        return by_stem[stem]
    if stem.endswith(".md") and stem[:-3] in by_stem:
        return by_stem[stem[:-3]]
    return None


def _extract_edges(ctx: VaultContext) -> tuple[list[str], list[Edge], set[tuple[str, str]]]:
    """遍历全部卡片 → (nodes, 显式边, 已连边对集合)。"""
    cards, by_stem, card_rels = _card_lookup(ctx)
    edges: list[Edge] = []
    taken: set[tuple[str, str]] = set()

    for w in cards:
        text = (ctx.root / w.rel).read_text(encoding="utf-8", errors="replace")
        for target in fa.extract_body_links(text):
            dst = resolve_target(target, w.rel, by_stem, card_rels)
            if dst and dst != w.rel:
                edges.append(Edge(w.rel, dst, "link"))
                taken.add((w.rel, dst))
            elif dst is None:
                edges.append(Edge(w.rel, target, "link", "dangling"))
        for target in parse_frontmatter_related(text):
            dst = resolve_target(target, w.rel, by_stem, card_rels)
            if dst and dst != w.rel:
                if (w.rel, dst) in taken:
                    continue  # 正文链接已表达同一关系
                edges.append(Edge(w.rel, dst, "frontmatter"))
                taken.add((w.rel, dst))
            elif dst is None:
                edges.append(Edge(w.rel, target, "frontmatter", "dangling"))
    return [w.rel for w in cards], edges, taken


def _semantic_edges(ctx: VaultContext, cards: list, taken: set[tuple[str, str]]) -> list[Edge]:
    """语义近邻边（§1.1 第 3 源）：仅 API Embedding 下开启，每卡至多 1 条出边。"""
    from core.rag.embedding import EmbeddingProvider

    if EmbeddingProvider().mode != "api" or len(cards) < 2:
        return []
    texts = [
        f"{w.title}\n{(ctx.root / w.rel).read_text(encoding='utf-8', errors='replace')}"
        for w in cards
    ]
    vecs = EmbeddingProvider().embed(texts)
    sim = vecs @ vecs.T
    edges: list[Edge] = []
    for i, w in enumerate(cards):
        best_j, best_s = -1, 0.0
        for j in range(len(cards)):
            if j == i or (w.rel, cards[j].rel) in taken:
                continue
            if float(sim[i][j]) > best_s:
                best_s, best_j = float(sim[i][j]), j
        if best_j >= 0 and best_s >= SEMANTIC_MIN_COS:
            edges.append(Edge(w.rel, cards[best_j].rel, "semantic", "ok", best_s))
    return edges


def build(ctx: VaultContext) -> RelationGraph:
    """全量构建（含语义边判定），不落盘。"""
    nodes, edges, taken = _extract_edges(ctx)
    cards, _, _ = _card_lookup(ctx)
    semantic = _semantic_edges(ctx, cards, taken)
    return RelationGraph(
        nodes=nodes,
        edges=edges + semantic,
        built_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        semantic_enabled=bool(semantic),
    )


# ---------------------------------------------------------------- 入口

def _resolve_ctx(source) -> VaultContext:
    """接受 VaultManager 或 VaultContext（检索层仅持有 ctx，管理入口传 vm）。"""
    if isinstance(source, VaultContext):
        return source
    return source.require()


def rebuild(vault_manager) -> RelationGraph:
    """全量重建并落盘 relations.json（卡片变更时调用；毫秒级，评审注 4）。"""
    ctx = _resolve_ctx(vault_manager)
    graph = build(ctx)
    f = relations_file(ctx.root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(graph.to_json(), ensure_ascii=False), encoding="utf-8")
    log_event(
        "GRAPH_REBUILD",
        nodes=len(graph.nodes),
        edges=len(graph.edges),
        dangling=sum(1 for e in graph.edges if e.status == "dangling"),
        semantic=graph.semantic_enabled,
    )
    return graph


def load(vault_manager) -> RelationGraph:
    """读取已持久化的关系图；缺失/损坏时自动全量重建（纯 Python，开销毫秒级）。"""
    ctx = _resolve_ctx(vault_manager)
    f = relations_file(ctx.root)
    if f.exists():
        try:
            return RelationGraph.from_json(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 — 图文件损坏按缺失处理
            pass
    return rebuild(vault_manager)
