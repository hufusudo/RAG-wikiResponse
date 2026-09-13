"""会话与全局回答风格 Prompt 的本地持久化（应用只写 wiki/ 铁律）。

存储位置（均位于应用可写区；`.llmwiki` 已被 wiki 扫描排除）：
    <vault>/wiki/.llmwiki/chats/<session_id>.json   单会话全量消息
    <vault>/wiki/.llmwiki/answer_style.md           当前仓库全局回答风格 Prompt

不维护独立索引：列表按文件现读现算（会话数量级小、单文件小），
与 relations.json 同策略——全量读写，避免增量同步 bug。
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from core.vault.vault_manager import WIKI_PREFIX, VaultContext

STATE_DIR = ".llmwiki"
CHATS_DIR = "chats"
PROMPT_FILE = "answer_style.md"
SESSION_ID_RE = re.compile(r"^[0-9a-z-]{8,64}$")

TITLE_MAX = 30
HISTORY_MAX_MESSAGES = 6  # 提供给 LLM 的最近历史条数（不含本轮提问）
HISTORY_MAX_CHARS = 1200  # 单条历史最大字符（防止上下文被历史撑爆）
PROMPT_MAX_CHARS = 8000


class ChatStoreError(Exception):
    """会话存储错误（非法 ID / 超长 Prompt 等，结构化返回前端）。"""


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


# ---------------------------------------------------------------- 内部工具

def _state_dir(ctx: VaultContext) -> Path:
    return ctx.root / WIKI_PREFIX / STATE_DIR


def _chats_dir(ctx: VaultContext) -> Path:
    return _state_dir(ctx) / CHATS_DIR


def _session_path(ctx: VaultContext, session_id: str) -> Path:
    if not SESSION_ID_RE.match(session_id or ""):
        raise ChatStoreError(f"非法会话 ID: {session_id!r}")
    return _chats_dir(ctx) / f"{session_id}.json"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _title_of(message: dict) -> str:
    text = " ".join(str(message.get("content", "")).split())
    if not text:
        return "新会话"
    return text[:TITLE_MAX] + ("…" if len(text) > TITLE_MAX else "")


def _summarize(session: dict) -> dict:
    return {
        "id": session["id"],
        "title": session.get("title") or "新会话",
        "created_at": session.get("created_at", 0),
        "updated_at": session.get("updated_at", 0),
        "message_count": sum(
            1 for m in session.get("messages", []) if m.get("role") in ("user", "assistant")
        ),
    }


# ---------------------------------------------------------------- 会话 CRUD

def load_session(ctx: VaultContext, session_id: str) -> dict | None:
    path = _session_path(ctx, session_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("id") != session_id:
        return None
    data.setdefault("messages", [])
    return data


def list_sessions(ctx: VaultContext) -> list[dict]:
    d = _chats_dir(ctx)
    if not d.is_dir():
        return []
    out = []
    for f in d.glob("*.json"):
        s = load_session(ctx, f.stem)
        if s:
            out.append(_summarize(s))
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return out


def append_message(ctx: VaultContext, session_id: str, message: dict) -> dict:
    """追加一条消息；会话不存在时以该消息（通常是首轮提问）为标题创建。"""
    session = load_session(ctx, session_id)
    now = time.time()
    if session is None:
        session = {
            "id": session_id,
            "title": _title_of(message),
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
    session["messages"].append({**message, "ts": message.get("ts", now)})
    session["updated_at"] = now
    _write_atomic(
        _session_path(ctx, session_id),
        json.dumps(session, ensure_ascii=False, indent=1),
    )
    return session


def delete_session(ctx: VaultContext, session_id: str) -> bool:
    path = _session_path(ctx, session_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def history_for_llm(ctx: VaultContext, session_id: str) -> list[dict]:
    """最近 N 条历史（不含刚写入的本轮提问），单条截断后供 LLM 多轮上下文使用。"""
    session = load_session(ctx, session_id)
    if not session:
        return []
    msgs = [
        m for m in session["messages"]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if msgs and msgs[-1]["role"] == "user":
        msgs = msgs[:-1]  # 本轮提问由 build_context 重新提供，避免重复
    msgs = msgs[-HISTORY_MAX_MESSAGES:]
    return [{"role": m["role"], "content": m["content"][:HISTORY_MAX_CHARS]} for m in msgs]


# ---------------------------------------------------------------- 全局风格 Prompt

def _prompt_path(ctx: VaultContext) -> Path:
    return _state_dir(ctx) / PROMPT_FILE


def get_prompt(ctx: VaultContext) -> str:
    p = _prompt_path(ctx)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_prompt(ctx: VaultContext, content: str) -> str:
    text = (content or "").strip()
    if len(text) > PROMPT_MAX_CHARS:
        raise ChatStoreError(f"风格 Prompt 过长（上限 {PROMPT_MAX_CHARS} 字）")
    p = _prompt_path(ctx)
    if not text:
        p.unlink(missing_ok=True)
        return ""
    _write_atomic(p, text)
    return text
