"""混合检索引擎（计划书 §8/§27 hybrid_engine.py）：BM25 + Dense + RRF。

选用 BM25L 变体：其 IDF 恒为正，在笔记级小语料（两篇文档共享常见词）
下不会像 BM25Okapi 那样产生负分导致排序反转。
"""
from __future__ import annotations

import re

import numpy as np
from rank_bm25 import BM25L

from core.rag.embedding import EmbeddingProvider

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z0-9_]+")
RRF_K = 60


def tokenize(text: str) -> list[str]:
    """中英文兼容分词：CJK 双字组 + 整词 + 拉丁小写词。"""
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
            tokens.append(run)  # 整词（利于标题精确命中，如「红黑树」）
    tokens.extend(w.lower() for w in _LATIN_RE.findall(text))
    return tokens


class HybridEngine:
    """对一组文档做 BM25 + Dense 检索并 RRF 融合排序。"""

    def __init__(self, docs: list[dict]):
        """docs: [{id, text, meta}]"""
        self.docs = docs
        self._emb = EmbeddingProvider()
        if docs:
            self._bm25 = BM25L([tokenize(d["text"]) for d in docs])
            self._doc_vecs = self._emb.embed([d["text"] for d in docs])
        else:
            self._bm25 = None
            self._doc_vecs = None

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        """RRF 融合排序；top_k=None 返回全部有分候选（由调用方做相关性门槛）。"""
        if not self._bm25:
            return []
        bm_scores = self._bm25.get_scores(tokenize(query))
        qvec = self._emb.embed([query])[0]
        dense_scores = self._doc_vecs @ qvec

        rrf: dict[int, float] = {}
        for scores in (bm_scores, dense_scores):
            for rank, idx in enumerate(np.argsort(scores)[::-1], start=1):
                if scores[idx] <= 0 and rank > 1:
                    continue
                rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank)

        ranked = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [
            {
                "id": self.docs[idx]["id"],
                "meta": self.docs[idx].get("meta", {}),
                "score": round(score, 6),
                "bm25": float(bm_scores[idx]),
                "dense": float(dense_scores[idx]),
            }
            for idx, score in ranked
        ]
