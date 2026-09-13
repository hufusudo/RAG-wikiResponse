"""问答 Agent（计划书 §16/§27 answer_agent.py）。

职责：Prompt 组装 / 上下文控制 / 五段式回答 / 覆盖声明 / SSE 流式输出。
"""
from __future__ import annotations

from core import llm
from core.rag.dag_search import RetrievalResult
from core.vault.vault_manager import VaultContext

MAX_RAW_CHARS = 12000   # 单篇 Raw 上限：默认给 1~2 篇完整文档而非 20 个 chunk
MAX_TOTAL_CHARS = 32000
RELATED_SNIPPET_CHARS = 800

SYSTEM_PROMPT = """你是「LLMwiki 智能伴学系统」的学术问答助手，用户正在用自己本地 Obsidian 笔记构建的知识库学习。

回答必须严格遵守以下五段 Markdown 结构：

## 核心概念
直接回答问题（核心定义 / 结论，2~5 句）。

## 笔记原文溯源
说明回答依据了哪些笔记（Wiki 卡片与 Raw 原文名称）。只能引用下方上下文中给出的文件，禁止编造文件名。

## 深度剖析
基于 Raw 原文的深入解释，可包含 LaTeX、推导、算法、简短代码、时间/空间复杂度。上下文中不存在的内容不要写进本节。

## 覆盖声明
明确区分两部分，各占一行：
- **来自知识库**：列出实际用到的笔记名（若无写"无"）。
- **模型补充**：明确指出哪些内容来自模型常识而非用户笔记（若无补充写"无"）。

## 追问引导
基于知识库内容给出 2~3 个继续学习的方向或问题。

其他要求：语言跟随提问（默认中文）；LaTeX 用 $...$；代码简短。"""

STYLE_PROMPT_HEADER = """【用户自定义回答风格（当前仓库全局设置，优先级高于默认风格）】
以下要求优先于上面的默认风格，但不得改变五段式结构，不得违反「只能引用上下文文件、禁止编造文件名」的溯源纪律：
{style}"""


def build_context(ctx: VaultContext, question: str, r: RetrievalResult) -> str:
    """§7 第五级：组装最终 LLM 上下文。"""
    parts: list[str] = [f"【用户问题】\n{question}"]

    if r.wiki_rels:
        parts.append("【命中的 Wiki 卡片】")
        for rel in r.wiki_rels:
            parts.append(f"### Wiki：{rel}\n{_read(ctx, rel)}")
    if r.related_rels:
        parts.append("【关联知识点（辅助）】")
        for rel in r.related_rels:
            parts.append(
                f"### Wiki：{rel}\n{_read(ctx, rel)[:RELATED_SNIPPET_CHARS]}…"
            )
    if r.raw_rels:
        parts.append("【Raw 完整原文（由 Wiki 卡片溯源定位）】")
        # Node 5（v2.2 §3.3）：按命中篇数均摊预算 PerPageLimit = max(2000, ⌊32k/N⌋)；
        # 单篇 ≤ 12k，总量 ≤ 32k；上游节点状态不回改（DAG 单向不可逆）。
        n_hits = max(1, len(r.wiki_rels) + len(r.related_rels) + len(r.raw_rels))
        per_page = max(2000, MAX_TOTAL_CHARS // n_hits)
        for rel in r.raw_rels:
            parts.append(
                f"#### {rel}\n{_read(ctx, rel)[:min(MAX_RAW_CHARS, per_page)]}"
            )
    if r.fallback_used:
        parts.append(
            "【检索说明】当前问题未命中任何 Wiki 卡片，"
            "以下 Raw 由标题匹配得到；回答的覆盖声明中请说明知识库尚无对应 Wiki。"
        )
    elif r.title_matched_rels:
        parts.append(
            "【检索说明】带「标题补充」的 Raw 由笔记标题与问题匹配得到"
            "（尚无对应 Wiki 卡片），其余 Raw 均由 Wiki 卡片溯源定位。"
        )
    return "\n\n".join(parts)


def _read(ctx: VaultContext, rel: str) -> str:
    return (ctx.root / rel).read_text(encoding="utf-8", errors="replace")


def build_messages(
    ctx: VaultContext,
    question: str,
    r: RetrievalResult,
    history: list[dict] | None = None,
    style_prompt: str | None = None,
) -> list[dict]:
    """系统提示 + 可选风格规范 + 历史轮次 + 本轮 RAG 上下文。

    【会话】history 为同一会话内最近若干轮问答（由 core.chat_store 提供并截断），
    使追问能沿用上下文；【风格】style_prompt 为当前仓库的全局回答风格设置。
    """
    system = SYSTEM_PROMPT
    if style_prompt and style_prompt.strip():
        system += "\n\n" + STYLE_PROMPT_HEADER.format(style=style_prompt.strip())
    messages: list[dict] = [{"role": "system", "content": system}]
    for m in history or []:
        role, content = m.get("role"), (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": build_context(ctx, question, r)})
    return messages


def stream_answer(messages: list[dict]):
    """透传 LLM token 流（由 api 层包装成 SSE）。"""
    yield from llm.chat_stream(messages)


def coverage_info(r: RetrievalResult) -> dict:
    return {
        "wiki_covered": bool(r.wiki_rels),
        "fallback_used": r.fallback_used,
    }


def sources_payload(ctx: VaultContext, r: RetrievalResult) -> list[dict]:
    """回答来源清单（wiki/raw 路径 + 是否已 Wiki 化）。"""
    have = {w.raw_rel for w in ctx.wiki_files if w.raw_rel}
    out = []
    for rel in r.wiki_rels:
        out.append({"rel": rel, "type": "wiki"})
    for rel in r.related_rels:
        out.append({"rel": rel, "type": "related"})
    for rel in r.raw_rels:
        out.append(
            {
                "rel": rel,
                "type": "raw",
                "wikified": rel in have,
                # 【v2.1】冷启动兜底 / 【v2.2】标题补充：凡非溯源来源一律标注，前端展示
                "fallback": r.fallback_used or rel in r.title_matched_rels,
            }
        )
    return out
