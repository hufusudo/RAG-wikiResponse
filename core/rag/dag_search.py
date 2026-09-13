"""五级 DAG 寻址管线（计划书 v2.2 §1.2，重构自 v2.1 page_table_search）。

「页表 / 寻址 / 穿透」仅为逻辑命名。整条问答寻址是**严格有向无环的数据流**：
每一级算子输入确定、输出可验证、单向不可逆（上游状态不回改）。

    [Node 1: Master 路由] → [Node 2: Wiki 卡片检索(门槛过滤)]
        → [Node 3: 拓扑有向边扩展(防环) | Node 3B: Raw 标题兜底]
        → [Node 4: 物理溯源映射] → (Node 5: 预算组装 → answer_agent)

防卡死三道刚性约束（§1.2 关键算法）在 relation_engine.related_cards 内实现：
    1. Hop 硬截断 = 1（O(1) 展开，不递归）
    2. Visited 集合 = 命中核心卡，出边目标重复即丢弃
    3. 关联卡配额 ≤ 2（仅取 800 字摘要，不读 Raw 全文）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.graph import format_adapter as fa
from core.graph import relation_engine
from core.log import log_event
from core.rag.hybrid_engine import HybridEngine, tokenize
from core.vault.vault_manager import VaultContext

WIKI_SECOND_MIN_RATIO = 0.25  # 命中卡相对首卡的 BM25 最低比值（纯陪跑通常为 0 或 <0.15）
MAX_HITS = 8                  # 安全帽：防超泛化查询拖入半个库（预算另由 32k 兜底）
MAX_RELATED = 2               # 关联卡配额（§1.2 约束 3）
RELATED_SNIPPET_CHARS = 800   # 关联卡只取核心摘要，不读 Raw 全文


@dataclass
class RouteStep:
    level: str      # master | sub | wiki | related | raw
    label: str
    detail: str = ""
    score: float | None = None

    def to_dict(self) -> dict:
        return {"level": self.level, "label": self.label,
                "detail": self.detail, "score": self.score}


@dataclass
class RetrievalResult:
    trace: list[RouteStep] = field(default_factory=list)
    wiki_rels: list[str] = field(default_factory=list)     # Node 2 命中的核心卡
    related_rels: list[str] = field(default_factory=list)  # Node 3 拓扑扩展（辅助）
    raw_rels: list[str] = field(default_factory=list)      # Node 4 溯源 Raw
    title_matched_rels: list[str] = field(default_factory=list)  # Node 3B 标题匹配进入的 Raw
    fallback_used: bool = False  # Node 3B：无 Wiki 覆盖时回退 Raw 标题匹配

    def trace_dicts(self) -> list[dict]:
        return [s.to_dict() for s in self.trace]


def _relevance_gate(hits: list[dict]) -> list[dict]:
    """Node 2 相关性门槛（计划书 §1.2）：每卡需 BM25 > 0 且 ≥ 首卡 × RATIO。

    「首卡」取 **BM25 真命中首卡**：dense-only（BM25=0）候选一律不算命中——
    本地哈希 Embedding 的字符重叠会制造纯陪跑首卡，既污染上下文，
    又会短路 Node 3B 兜底（回归：问「方向导数」被同目录 dense 陪跑卡遮蔽）。
    """
    if not hits:
        return []
    real = [h for h in hits if h["bm25"] > 0]
    if not real:
        return []
    kept = [real[0]]
    ref = real[0]["bm25"]
    for h in real[1:]:
        if len(kept) >= MAX_HITS:
            break
        if h["bm25"] >= WIKI_SECOND_MIN_RATIO * ref:
            kept.append(h)
    return kept


def card_raw_rel(ctx: VaultContext, wiki_rel: str) -> str | None:
    """Node 4：从卡片溯源行解析 Raw 路径（仓库根相对路径；不存在则 None）。

    双格式兼容（v2.2 §2.1）：标准 Markdown 链接与旧 Obsidian 双链均支持。
    """
    p = ctx.root / wiki_rel
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    target = fa.parse_raw_rel(text, card_rel=wiki_rel)
    if not target:
        return None
    if (ctx.root / target).exists():
        return target
    log_event("RAW_TRACE_MISS", card=wiki_rel, target=target)  # 溯源悬空可观测（用户移动 Raw 等）
    return None


def _card_text(ctx: VaultContext, rel: str) -> str:
    return (ctx.root / rel).read_text(encoding="utf-8", errors="replace")


class DagSearch:
    """一次 retrieve 完成五级 DAG 寻址（Node 5 组装 / Node 6 生成由 answer_agent 承接）。"""

    def __init__(self, ctx: VaultContext):
        self.ctx = ctx
        cards = [w for w in ctx.wiki_files if not w.is_index]
        self.card_meta = {w.rel: w for w in cards}

        # Node 2 语料：Wiki Card（标题 + 正文；**剥离溯源行**——它是系统元数据，
        # 不参与相关性计算。回归：溯源行的「原始笔记/原文」会让任意查询给所有卡片
        # 白送 BM25 分，陪跑卡由此混过门槛并遮蔽 Node 3B 兜底）
        self.wiki_engine = HybridEngine(
            [
                {"id": w.rel,
                 "text": f"{w.title}\n{fa.strip_trace(_card_text(ctx, w.rel))}",
                 "meta": {"subject": w.subject, "title": w.title}}
                for w in cards
            ]
        )
        # Node 1 语料：Master（学科 + 该学科所有卡片标题）
        self.master_engine = HybridEngine(
            [
                {
                    "id": s,
                    "text": s + " " + " ".join(
                        w.title for w in cards if w.subject == s
                    ),
                    "meta": {"subject": s},
                }
                for s in ctx.subjects
            ]
        )
        # Node 3B 语料：Raw 标题（冷启动 / 无 Wiki 覆盖时）
        self.raw_engine = HybridEngine(
            [
                {"id": r.rel,
                 "text": f"{r.subject} {r.name} {r.title}",
                 "meta": {"subject": r.subject, "title": r.title}}
                for r in ctx.raw_files
            ]
        )
        # Node 3B 标题语料 + 词项文档频率表（df）：区分稀有主题词与「笔记」类泛词
        self.raw_title_text = {
            r.rel: f"{r.subject} {r.name} {r.title}" for r in ctx.raw_files
        }
        self._title_df: dict[str, int] = {}
        for text in self.raw_title_text.values():
            for t in set(tokenize(text)):
                self._title_df[t] = self._title_df.get(t, 0) + 1
        # Node 3 语料：静态概念有向图（relations.json；缺失时自动重建）
        self.graph = relation_engine.load(ctx)

    # -------------------------------------------------------------

    def _title_relevant(self, query: str, title_text: str) -> bool:
        """标题级相关性判据：标题与查询的共享词项中存在**稀有主题词**。

        df 阈值 = max(2, 语料数/50)：df(「中断」)=1 → 主题词收；
        df(「笔记」) dozens（用户库大量「笔记.md」）→ 查询口语词"我笔记中"命中也拒。
        """
        common = set(tokenize(title_text)) & set(tokenize(query))
        if not common:
            return False
        threshold = max(2, len(self.raw_title_text) // 50)
        return any(self._title_df.get(t, 0) <= threshold for t in common)

    def retrieve(self, query: str) -> RetrievalResult:
        res = RetrievalResult(trace=[RouteStep("master", "总索引", "wiki/INDEX.md")])

        # ---- Node 1：Master 路由（学科 Top 1~2）
        subjects = self.master_engine.search(query, top_k=2)
        focus: set[str] | None = None
        if subjects:
            focus = {s["id"] for s in subjects}
            top = subjects[0]
            res.trace.append(
                RouteStep(
                    "sub",
                    f"子索引：{top['id']}",
                    detail="、".join(sorted(focus)),
                    score=top["score"],
                )
            )

        # ---- Node 2：Wiki 卡片检索（门槛内全部命中，无篇数硬上限）
        wiki_hits = self.wiki_engine.search(query)
        if focus:
            wiki_hits = [h for h in wiki_hits if h["meta"].get("subject") in focus] or wiki_hits
        wiki_hits = _relevance_gate(wiki_hits)
        for h in wiki_hits:
            res.wiki_rels.append(h["id"])
            res.trace.append(
                RouteStep("wiki", f"Wiki：{h['meta'].get('title', h['id'])}",
                          detail=h["id"], score=h["score"])
            )

        # ---- Node 3 / 3B：拓扑有向边扩展（防环）或 Raw 标题兜底
        if res.wiki_rels:
            for rel in self.graph.related_cards(res.wiki_rels, quota=MAX_RELATED):
                res.related_rels.append(rel)
                w = self.card_meta.get(rel)
                res.trace.append(
                    RouteStep("related", f"关联：{w.title if w else rel}", detail=rel)
                )
        if not res.wiki_rels:
            res.fallback_used = True  # 冷启动：Wiki 层零真命中 → 标题匹配即兜底
        # 标题匹配始终执行：零命中时是兜底；有命中时是「补充」——
        # 未 Wiki 化但标题强相关的 Raw 不得被部分相关的卡片遮蔽
        # （回归：问「方向导数」时被同目录卡片正文的「方向」二字遮蔽）。
        # 收紧：top_k=4 + 门槛要求标题真实词命中（bm25>0，gate 已保证），
        # 否则宽泛问题拉进噪声笔记；随后由数据飞轮为命中 Raw 补卡。
        for h in _relevance_gate(self.raw_engine.search(query, top_k=4)):
            if h["id"] in res.raw_rels:
                continue  # 已由卡片溯源定位，去重
            if not self._title_relevant(query, self.raw_title_text.get(h["id"], "")):
                continue  # 泛化标题被口语词命中的噪音（如「笔记.md」×"我笔记中"）
            res.raw_rels.append(h["id"])
            res.title_matched_rels.append(h["id"])
            tag = "兜底·标题匹配" if res.fallback_used else "标题补充"
            res.trace.append(
                RouteStep("raw", f"Raw：{h['id']}（{tag}）",
                          detail=h["id"], score=h["score"])
            )

        # ---- Node 4：物理溯源映射（只读「过门槛的命中卡」的 Raw 全文；
        # 关联卡仅作辅助摘要，不因一条边把大量笔记送进 LLM）
        if res.wiki_rels:
            for rel in res.wiki_rels:
                raw_rel = card_raw_rel(self.ctx, rel)
                if raw_rel and raw_rel not in res.raw_rels:
                    res.raw_rels.append(raw_rel)
                    res.trace.append(RouteStep("raw", f"Raw：{raw_rel}", detail=raw_rel))

        log_event(
            "RAG_ROUTE", q=query[:50], wiki=res.wiki_rels,
            related=res.related_rels, raw=res.raw_rels, fallback=res.fallback_used,
        )
        return res


def build_search(ctx: VaultContext) -> DagSearch:
    """构造（或复用）与当前 Vault 状态同步的检索器。

    缓存键 = (id(ctx), wiki 文件数)；Wiki 数量变化即重建，保证新卡片可检索
    （卡片保存路径会全量重建 relations.json，此处重建时自然加载新图）。
    """
    global _CACHE
    key = (id(ctx), ctx.wiki_count)
    if _CACHE.get("key") != key:
        _CACHE = {"key": key, "engine": DagSearch(ctx)}
    return _CACHE["engine"]


_CACHE: dict = {}
