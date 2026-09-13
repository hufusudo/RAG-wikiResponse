"""全部 HTTP 接口（计划书 §11~§17/§31 MVP）。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config.settings import settings
from core import chat_store, llm
from core.chat_store import ChatStoreError
from core.generator import answer_agent
from core.generator.flywheel_agent import FlywheelAgent
from core.graph import relation_engine
from core.indexer import two_level_builder
from core.indexer.auto_wiki_ai import (
    DistillTaskManager,
    WikiValidationError,
    distill,
    save_wiki,
)
from core.log import log_event
from core.rag.dag_search import build_search
from core.vault.raw_guardian import RawGuardError
from core.vault.vault_manager import VaultError, VaultManager

APP_VERSION = "0.1.0"

router = APIRouter(prefix="/api")
vault_manager = VaultManager()
task_manager = DistillTaskManager(vault_manager)
flywheel = FlywheelAgent(task_manager)


# ---------------------------------------------------------------- models

class VaultOpenBody(BaseModel):
    path: str
    layout: dict | None = None  # 【v2.1】可选手动布局声明 {"subjects": {…}, "excludes": […]}


class SettingsBody(BaseModel):
    values: dict


class RawPathBody(BaseModel):
    raw_path: str


class WikiSaveBody(BaseModel):
    raw_path: str
    content: str


class BatchBody(BaseModel):
    scope: str = "all"
    subject: str | None = None


class LayoutConfirmBody(BaseModel):
    """【v2.1】固化布局检测结果为手动声明（写入 wiki/.llmwiki/layout.json）。"""
    subjects: dict[str, str]
    excludes: list[str] = []


class ChatBody(BaseModel):
    message: str
    session_id: str | None = None  # 【v2.3】空 = 新建会话；有值 = 继续该会话


class PromptBody(BaseModel):
    """【v2.3】当前仓库全局回答风格 Prompt（清空则删除）。"""
    content: str = ""


# ---------------------------------------------------------------- helpers

def _ctx():
    return vault_manager.require()


def _safe_read(vault_rel: str) -> tuple[Path, str]:
    """只读访问源笔记（任意布局，以 manifest 为准）或 wiki/ 下的文件（防越界）。"""
    ctx = _ctx()
    vault_rel = vault_rel.strip("/")
    if ".." in vault_rel or not (vault_rel.startswith("wiki/") or vault_rel in ctx.manifest):
        raise HTTPException(400, detail=f"非法路径: {vault_rel}")
    p = (ctx.root / vault_rel).resolve()
    if not str(p).startswith(str(ctx.root.resolve())):
        raise HTTPException(400, detail=f"路径越界: {vault_rel}")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, detail=f"文件不存在: {vault_rel}")
    return p, vault_rel


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------- 基础

@router.get("/version")
def version():
    return {"name": "LLMwiki 智能伴学系统", "version": APP_VERSION}


# ---------------------------------------------------------------- Settings

@router.get("/settings")
def get_settings():
    return {"success": True, "settings": settings.as_dict(mask_secrets=True)}


@router.post("/settings")
def update_settings(body: SettingsBody):
    try:
        data = settings.update(body.values)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    log_event("SETTINGS_UPDATE", keys=sorted(body.values.keys()))
    return {"success": True, "settings": data}


@router.post("/settings/test")
def test_settings():
    return llm.test_connection()


@router.get("/llm/models")
def llm_models():
    """拉取可用模型列表（OpenAI 兼容 /models；设置保存后前端自动调用）。"""
    if not settings.is_set("LLM_API_KEY"):
        raise HTTPException(400, detail="未配置 LLM_API_KEY，请先在设置中填写")
    try:
        return {"success": True, "models": llm.list_models(), "current": llm.get_model()}
    except llm.LLMError as e:
        raise HTTPException(502, detail=str(e))


# ---------------------------------------------------------------- 文件夹选择器

@router.get("/fs/list")
def fs_list(path: str | None = None):
    """本地目录浏览（前端「选择 Vault 文件夹」对话框的数据源；本机部署，路径即磁盘）。

    只列子目录（Vault 选择不涉及文件）；隐藏目录（. 开头）不展示，
    需要时可经路径输入框手输进入。
    """
    target = Path(path).expanduser() if path else Path.home()
    if not target.exists():
        raise HTTPException(404, detail=f"路径不存在: {target}")
    if not target.is_dir():
        raise HTTPException(400, detail=f"不是目录: {target}")
    resolved = target.resolve()
    dirs = sorted(
        (p.name for p in resolved.iterdir()
         if p.is_dir() and not p.name.startswith(".")),
        key=str.lower,
    )
    parent = resolved.parent
    return {
        "success": True,
        "path": str(resolved),
        "parent": str(parent) if parent != resolved else None,  # 到根后无上级
        "home": str(Path.home()),
        "dirs": dirs,
    }


# ---------------------------------------------------------------- Vault

@router.post("/vault/open")
def vault_open(body: VaultOpenBody):
    try:
        ctx = vault_manager.open(body.path, body.layout)
    except VaultError as e:
        return {"success": False, "error": str(e)}
    # 记住当前 Vault（写入 .env，下次启动自动恢复；不硬编码路径）
    settings.update({"VAULT_PATH": str(ctx.root)})
    return {
        "success": True,
        "vault_name": ctx.name,
        "path": str(ctx.root),
        "layout": ctx.plan.as_dict(),  # 【v2.1】布局检测结果（供前端确认/展示）
        **ctx.stats(),
        "external_changes": ctx.external_changes[:20],
        "settings": settings.as_dict(mask_secrets=True),
    }


@router.post("/vault/layout/confirm")
def vault_layout_confirm(body: LayoutConfirmBody):
    """【v2.1】固化布局检测结果为手动声明；客户端随后重新 open 生效。"""
    ctx = _ctx()
    if not body.subjects:
        raise HTTPException(400, detail="subjects 不能为空")
    f = ctx.root / "wiki" / ".llmwiki" / "layout.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "mode": "manual",
        "subjects": body.subjects,
        "excludes": body.excludes,
    }
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"success": True, "layout": data, "hint": "重新打开 Vault 后生效"}


@router.get("/vault/state")
def vault_state():
    ctx = vault_manager.get()
    if ctx is None:
        return {"success": True, "opened": False, "settings": settings.as_dict()}
    return {
        "success": True,
        "opened": True,
        "vault_name": ctx.name,
        "path": str(ctx.root),
        **ctx.stats(),
        "settings": settings.as_dict(mask_secrets=True),
    }


# ---------------------------------------------------------------- 会话与风格（每仓库）

@router.get("/chat/sessions")
def chat_sessions():
    """当前仓库的历史会话列表（按最近更新倒序）。"""
    return {"success": True, "sessions": chat_store.list_sessions(_ctx())}


@router.get("/chat/sessions/{session_id}")
def chat_session_detail(session_id: str):
    try:
        session = chat_store.load_session(_ctx(), session_id)
    except ChatStoreError as e:
        raise HTTPException(400, detail=str(e))
    if session is None:
        raise HTTPException(404, detail=f"会话不存在: {session_id}")
    return {"success": True, "session": session}


@router.delete("/chat/sessions/{session_id}")
def chat_session_delete(session_id: str):
    try:
        removed = chat_store.delete_session(_ctx(), session_id)
    except ChatStoreError as e:
        raise HTTPException(400, detail=str(e))
    if not removed:
        raise HTTPException(404, detail=f"会话不存在: {session_id}")
    log_event("CHAT_SESSION_DELETE", session=session_id)
    return {"success": True, "removed": True}


@router.get("/prompt")
def get_prompt():
    return {"success": True, "prompt": chat_store.get_prompt(_ctx())}


@router.post("/prompt")
def update_prompt(body: PromptBody):
    try:
        text = chat_store.set_prompt(_ctx(), body.content)
    except ChatStoreError as e:
        raise HTTPException(400, detail=str(e))
    log_event("PROMPT_UPDATE", chars=len(text))
    return {"success": True, "prompt": text}


# ---------------------------------------------------------------- Indexer

@router.post("/indexer/init")
def indexer_init():
    stats = two_level_builder.init_indexes(vault_manager)
    return {"success": True, **stats}


@router.get("/graph/lint")
def graph_lint():
    """知识库拓扑巡检（v2.2 §2.2）：孤岛概念（入度=0）与悬空边报告。"""
    graph = relation_engine.load(vault_manager)
    return {"success": True, **graph.lint()}


@router.get("/index/tree")
def index_tree():
    return {"success": True, "tree": two_level_builder.get_tree(_ctx())}


# ---------------------------------------------------------------- Wiki

@router.get("/wiki/missing")
def wiki_missing(subject: str | None = None):
    """未 Wiki 化 raw 清单；size_bytes 供前端估算批量蒸馏的 Token 成本。"""
    ctx = _ctx()
    have = {w.raw_rel for w in ctx.wiki_files if w.raw_rel}
    items, total_size = [], 0
    for r in ctx.raw_files:
        if r.rel in have or (subject is not None and r.subject != subject):
            continue
        size = ctx.manifest.get(r.rel, {}).get("size", 0)
        total_size += size
        items.append(
            {
                "raw_path": r.rel,
                "title": r.title,
                "subject": r.subject,
                "size_bytes": size,
            }
        )
    return {
        "success": True,
        "count": len(items),
        "items": items,
        "total_size_bytes": total_size,
    }


@router.get("/file/content")
def file_content(path: str):
    p, rel = _safe_read(path)
    return {"success": True, "path": rel, "content": p.read_text(encoding="utf-8", errors="replace")}


@router.post("/wiki/distill")
def wiki_distill(body: RawPathBody):
    try:
        draft = distill(_ctx(), body.raw_path)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {"success": True, **draft}


@router.post("/wiki/save")
def wiki_save(body: WikiSaveBody):
    try:
        result = save_wiki(vault_manager, body.raw_path, body.content)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {"success": True, **result}


@router.post("/wiki/batch")
def wiki_batch(body: BatchBody):
    task_id = task_manager.start_batch(scope=body.scope, subject=body.subject)
    return {"success": True, "task_id": task_id}


@router.get("/wiki/tasks")
def wiki_tasks():
    return {"success": True, "tasks": task_manager.list_tasks()}


@router.get("/wiki/task/{task_id}")
def wiki_task(task_id: str):
    try:
        return {"success": True, **task_manager.get(task_id)}
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


# ---------------------------------------------------------------- Chat (SSE)

@router.post("/chat")
def chat(body: ChatBody):
    message = body.message.strip()
    if not message:
        raise HTTPException(400, detail="问题不能为空")
    try:
        ctx = _ctx()
    except VaultError as e:
        raise HTTPException(400, detail=str(e))
    if not settings.get("LLM_API_KEY"):
        raise HTTPException(
            400, detail="未配置 LLM_API_KEY，请先在「模型与 API Key 设置」中填写"
        )

    # 会话落盘 + 多轮上下文（先追加本轮 user，再取历史；写入失败不影响问答）
    session_id = body.session_id or chat_store.new_session_id()
    try:
        chat_store.append_message(ctx, session_id, {"role": "user", "content": message})
        history = chat_store.history_for_llm(ctx, session_id)
    except ChatStoreError as e:
        raise HTTPException(400, detail=str(e))

    # 检索与上下文组装在响应前完成；只有 token 流进入 SSE
    search = build_search(ctx)
    retrieval = search.retrieve(message)
    messages = answer_agent.build_messages(
        ctx, message, retrieval,
        history=history,
        style_prompt=chat_store.get_prompt(ctx),
    )
    trace = retrieval.trace_dicts()
    coverage = answer_agent.coverage_info(retrieval)
    sources = answer_agent.sources_payload(ctx, retrieval)

    def gen():
        yield _sse("route", {"trace": trace, "coverage": coverage, "session_id": session_id})
        n_tokens, pieces, error = 0, [], None
        try:
            for delta in answer_agent.stream_answer(messages):
                n_tokens += 1
                pieces.append(delta)
                yield _sse("token", {"delta": delta})
        except llm.LLMError as e:
            error = str(e)
            yield _sse("error", {"error": error})
        # 助手消息落盘（含来源；出错时保留已生成部分，便于回看）
        try:
            chat_store.append_message(ctx, session_id, {
                "role": "assistant",
                "content": "".join(pieces),
                "sources": sources,
                "coverage": coverage,
                **(({"error": error}) if error else {}),
            })
        except ChatStoreError:
            log_event("CHAT_SAVE_FAIL", session=session_id)
        if error:
            return
        lazy = flywheel.after_chat(ctx, retrieval.raw_rels)
        yield _sse(
            "done",
            {
                "sources": sources,
                "coverage": coverage,
                "session_id": session_id,
                "lazy_task": lazy,
            },
        )
        log_event("CHAT_DONE", q=message[:50], tokens=n_tokens, lazy=bool(lazy), session=session_id)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
