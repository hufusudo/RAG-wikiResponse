"""Embedding 统一 Provider（计划书 §10/§27 embedding.py）。

- 已配置 EMBEDDING_BASE_URL / EMBEDDING_API_KEY → OpenAI 兼容 API
- 未配置 → 本地离线 Embedding（字符 n-gram 哈希，无外部依赖，支持中英文）
- API 失败时自动降级到本地，保证检索可用
"""
from __future__ import annotations

import zlib

import numpy as np

from config.settings import settings
from core.log import log_event

DIM = 256
API_BATCH = 32


def _feats(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            buf += ch
        else:
            if buf:
                out.extend(_cjk_feats(buf))
                buf = ""
            if ch.isascii() and (ch.isalnum()):
                out.append(ch.lower())
    if buf:
        out.extend(_cjk_feats(buf))
    return out


def _cjk_feats(run: str) -> list[str]:
    feats = [run[i:i + 2] for i in range(len(run) - 1)]
    feats.append(run)
    return feats


def local_embed(texts: list[str]) -> np.ndarray:
    """哈希字符 n-gram → L2 归一化矩阵 (n, DIM)。"""
    mat = np.zeros((len(texts), DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        for f in _feats(t):
            mat[i, zlib.crc32(f.encode("utf-8")) % DIM] += 1.0
    norm = np.linalg.norm(mat, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    return mat / norm


class EmbeddingProvider:
    @property
    def mode(self) -> str:
        return "api" if (settings.get("EMBEDDING_BASE_URL") or settings.get("EMBEDDING_API_KEY")) else "local"

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        if self.mode == "api":
            try:
                return self._embed_api(texts)
            except Exception as e:  # noqa: BLE001
                log_event("EMBEDDING_API_FAIL", error=str(e), fallback="local")
        return local_embed(texts)

    def _embed_api(self, texts: list[str]) -> np.ndarray:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.get("EMBEDDING_API_KEY") or "EMPTY",
            base_url=settings.get("EMBEDDING_BASE_URL"),
        )
        model = settings.get("EMBEDDING_MODEL") or "text-embedding-3-small"
        vecs: list[list[float]] = []
        for i in range(0, len(texts), API_BATCH):
            resp = client.embeddings.create(
                model=model, input=texts[i:i + API_BATCH]
            )
            vecs.extend(d.embedding for d in resp.data)
        mat = np.array(vecs, dtype=np.float32)
        norm = np.linalg.norm(mat, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return mat / norm
