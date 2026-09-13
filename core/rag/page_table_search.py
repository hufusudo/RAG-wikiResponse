"""兼容垫片（v2.2）：实现已迁移至 core/rag/dag_search.py 五级 DAG 管线。

保留本模块别名，存量调用（tests / answer_agent / 外部脚本）无需改动；
新代码请直接 `from core.rag.dag_search import ...`。
"""
from core.rag.dag_search import (  # noqa: F401
    MAX_HITS,
    RELATED_SNIPPET_CHARS,
    WIKI_SECOND_MIN_RATIO,
    DagSearch,
    RetrievalResult,
    RouteStep,
    _relevance_gate,
    build_search,
    card_raw_rel,
)

PageTableSearch = DagSearch  # 兼容别名
__all__ = [
    "DagSearch",
    "PageTableSearch",
    "RetrievalResult",
    "RouteStep",
    "build_search",
    "card_raw_rel",
    "_relevance_gate",
    "MAX_HITS",
    "WIKI_SECOND_MIN_RATIO",
    "RELATED_SNIPPET_CHARS",
]
