"""Wiki 精炼系统（计划书 §9/§27 auto_wiki_ai.py）。

统一入口 distill_and_save(raw_path)，三种触发方式复用同一逻辑：
  A. Lazy Distillation   —— 问答后由 flywheel_agent 后台触发
  B. Async Batch Queue   —— 批量初始化，线程任务队列 + 进度查询 + 断点续传
  C. Interactive Curate  —— 返回草稿给前端编辑后确认保存

约束（§5）：必须有 Raw 溯源行；是摘要不是副本（25~40 行目标）；
不生成悬空双链（生成后把无效 [[链接]] 降级为普通文本，v2.2 §2.2 记录为悬空边）。

v2.2：溯源行默认输出标准 Markdown 相对链接（卡片目录 → Raw）,
可经 OBSIDIAN_COMPATIBILITY 开关回到旧双链格式；关系图在卡片保存后全量重建。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings
from core import llm
from core.graph import format_adapter as fa
from core.graph import relation_engine
from core.indexer import two_level_builder
from core.log import log_event
from core.vault.raw_guardian import STATE_DIR, RawGuardError, write_wiki_file
from core.vault.vault_manager import (
    INDEX_NAME,
    RAW_PREFIX,
    VaultContext,
    VaultManager,
    WIKI_PREFIX,
)

MAX_RAW_CHARS = 12000          # 单篇 Raw 送入 LLM 的字符上限
MAX_TITLES_IN_PROMPT = 150     # 提供给 LLM 的已有知识点标题数量上限
TARGET_MIN_LINES, TARGET_MAX_LINES = 25, 40


class WikiValidationError(Exception):
    def __init__(self, msg: str, warnings: list[str] | None = None):
        super().__init__(msg)
        self.warnings = warnings or []


# ------------------------------------------------------------------ 工具

def wiki_rel_for(raw: "RawFile") -> str:
    """卡片路径 = wiki/<学科显示名>/<rel 去掉 subject_dir 前缀>（设计方案 §5）。

    - standard：raw/数据结构/红黑树.md → wiki/数据结构/红黑树.md（与 v2.0 镜像一致）
    - per_subject：概率论wiki/章节/a.md → wiki/概率论/章节/a.md
    - 无学科笔记：TODO.md → wiki/TODO.md；raw/散章.md → wiki/散章.md
    """
    rel = raw.rel
    if raw.subject_dir and rel.startswith(raw.subject_dir + "/"):
        under = rel[len(raw.subject_dir) + 1:]
    else:
        under = rel
    if raw.subject:
        return f"{WIKI_PREFIX}/{raw.subject}/{under}"
    return f"{WIKI_PREFIX}/{under}"


def existing_wiki_titles(ctx: VaultContext) -> set[str]:
    titles = {w.name for w in ctx.wiki_files if not w.is_index}
    titles |= set(ctx.subjects)
    return titles


def _resolve_dangling_links(text: str, ctx: VaultContext) -> tuple[str, list[str]]:
    """把指向不存在知识点的双链降级为普通文本（§5.3 禁止悬空双链）。

    溯源行（> 📖 原始笔记：[[raw/...]]）不参与降级 —— 它指向 Raw 层而非 Wiki 层。
    """
    valid = existing_wiki_titles(ctx)
    removed: list[str] = []

    def repl(m: re.Match) -> str:
        target = m.group(1).split("|", 1)[0].strip()
        name = target.split("/")[-1]
        if target in valid or name in valid or target.endswith("/" + INDEX_NAME[:-3]):
            return m.group(0)
        removed.append(target)
        return m.group(1).split("|", 1)[-1].strip()

    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if fa.is_trace_line(line.rstrip("\n")):
            out.append(line)  # 溯源行原样保留
            continue
        out.append(fa.WIKILINK_RE.sub(repl, line))
    return "".join(out), removed


def validate_card(content: str, ctx: VaultContext) -> list[str]:
    """校验 Wiki Card 约束，返回 warnings（空 = 全部通过）。"""
    warnings: list[str] = []
    n_lines = len(content.strip().splitlines())
    if not (TARGET_MIN_LINES <= n_lines <= TARGET_MAX_LINES + 15):
        warnings.append(
            f"卡片行数 {n_lines} 行，目标区间 {TARGET_MIN_LINES}~{TARGET_MAX_LINES} 行"
        )
    for section in ("一句话本质", "核心要点", "相关知识点"):
        if section not in content:
            warnings.append(f"缺少章节「{section}」")
    _, dangling = _resolve_dangling_links(content, ctx)
    if dangling:
        warnings.append(f"已移除悬空双链: {dangling}")
    return warnings


# ------------------------------------------------------------------ 精炼

def _trace_mode() -> bool:
    """溯源行输出格式开关（v2.2 §2.1）：true = 旧 Obsidian 双链；默认标准 Markdown。"""
    return (settings.get("OBSIDIAN_COMPATIBILITY") or "").lower() == "true"


def _build_distill_messages(ctx: VaultContext, raw_rel: str) -> tuple[list[dict], str]:
    raw = ctx.raw_by_rel().get(raw_rel)
    if raw is None:
        raise ValueError(f"Raw 文件不存在: {raw_rel}")
    raw_text = (ctx.root / raw_rel).read_text(encoding="utf-8", errors="replace")
    truncated = ""
    if len(raw_text) > MAX_RAW_CHARS:
        raw_text = raw_text[:MAX_RAW_CHARS]
        truncated = "…（原文过长已截断，只提炼已给内容）"

    titles = sorted(existing_wiki_titles(ctx) - {raw.name})
    shown = titles[:MAX_TITLES_IN_PROMPT]
    note = "" if len(titles) <= MAX_TITLES_IN_PROMPT else f"（仅列出前 {len(shown)} 个）"
    trace = fa.build_trace_line(wiki_rel_for(raw), raw_rel, raw.title, obsidian=_trace_mode())

    system = (
        "你是学术笔记编辑，负责把用户的原始笔记提炼成 Obsidian 风格的 Wiki Card。"
        "只输出卡片 Markdown 本身，不要任何解释、代码块包裹或开场白。\n"
        "硬性要求：\n"
        "1. 第一行必须是 `# {知识点名称}`；\n"
        f"2. 第二段必须是溯源行：`{trace}`（原样输出，不要改动）；\n"
        "3. 之后依次为章节：`## 一句话本质`、`## 核心要点`（3~5 条，最后一条写边界条件/易错点/复杂度）、`## 相关知识点`；\n"
        f"4. 「相关知识点」中只允许使用下方给出的已有知识点标题做双链{note}；"
        "没有真正相关的概念就省略该小节，绝不创造不存在的 [[链接]]；\n"
        "5. 卡片总体 25~40 行：提炼与导航，不要复制大段代码、推导草稿或整篇笔记；\n"
        "6. 语言跟随原始笔记（默认中文）。"
    )
    user = (
        f"【原始笔记：{raw.title}】\n{raw_text}{truncated}\n\n"
        f"【当前知识库已有知识点（可双链）】\n{('、'.join(shown)) if shown else '（暂无其他知识点）'}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], raw.title


def distill(ctx: VaultContext, raw_rel: str) -> dict:
    """生成 Wiki 草稿（不落盘）。返回 {raw_rel, title, draft, warnings}。"""
    log_event("WIKI_DISTILL_START", raw=raw_rel, mode="draft")
    messages, title = _build_distill_messages(ctx, raw_rel)
    text = llm.chat_complete(messages, temperature=0.2, max_tokens=2000)

    # 去掉 LLM 自己输出的标题与溯源行，统一重写规范头部（保证溯源正确）
    body = text.strip()
    body = re.sub(r"^#\s+.*?\n", "", body, count=1)
    body = fa.strip_trace(body)
    trace = fa.build_trace_line(
        wiki_rel_for(ctx.raw_by_rel()[raw_rel]), raw_rel, title, obsidian=_trace_mode()
    )
    draft = f"# {title}\n\n{trace}\n\n{body}\n"
    draft, dangling = _resolve_dangling_links(draft, ctx)
    warnings = validate_card(draft, ctx)
    if dangling:
        warnings = [w for w in warnings if not w.startswith("已移除悬空双链")]
        warnings.append(f"已移除悬空双链: {dangling}")
    return {"raw_rel": raw_rel, "title": title, "draft": draft, "warnings": warnings}


def save_wiki(vault_manager: VaultManager, raw_rel: str, content: str) -> dict:
    """校验并写入 wiki/（唯一合法写入口），随后增量更新两级索引。"""
    ctx = vault_manager.require()
    raw = ctx.raw_by_rel().get(raw_rel)
    if raw is None:
        raise WikiValidationError(f"Raw 笔记不存在: {raw_rel}")
    wiki_rel = wiki_rel_for(raw)
    if not fa.parse_raw_rel(content, wiki_rel):
        raise WikiValidationError(
            "Wiki Card 缺少 Raw 溯源行（> 📖 原始笔记：[…](…) 或 [[…]]），拒绝保存"
        )
    content, dangling = _resolve_dangling_links(content, ctx)
    warnings = validate_card(content, ctx)

    write_wiki_file(
        ctx.root, wiki_rel, content, manifest=ctx.manifest, raw_rel=raw_rel
    )
    vault_manager.refresh_wiki_only()
    relation_engine.rebuild(vault_manager)  # v2.2：卡片变更 → 关系图全量重建（评审注 4）
    subject = raw.subject or None  # 学科显示名（手动/frontmatter/目录派生 合成后）
    two_level_builder.update_indexes_for(vault_manager, subject)
    log_event(
        "WIKI_DISTILL_COMPLETE", raw=raw_rel, wiki=wiki_rel, warnings=len(warnings)
    )
    return {"raw_rel": raw_rel, "wiki_rel": wiki_rel, "warnings": warnings}


def distill_and_save(vault_manager: VaultManager, raw_rel: str) -> dict:
    """后台任务使用：生成 + 落盘一步完成。"""
    ctx = vault_manager.require()
    d = distill(ctx, raw_rel)
    return save_wiki(vault_manager, raw_rel, d["draft"])


# ------------------------------------------------------------- 任务管理

@dataclass
class TaskRecord:
    task_id: str
    mode: str  # batch | lazy
    scope: str = "all"
    subject: str | None = None
    status: str = "running"  # running | finished
    total: int = 0
    completed: int = 0
    current_file: str | None = None
    errors: list[dict] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def view(self) -> dict:
        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "scope": self.scope,
            "subject": self.subject,
            "status": self.status,
            "total": self.total,
            "completed": self.completed,
            "errors": len(self.errors),
            "error_samples": self.errors[:10],
            "current_file": self.current_file,
            "percent": int(self.completed * 100 / self.total) if self.total else 0,
        }


class DistillTaskManager:
    """后台蒸馏任务队列（线程执行，不阻塞问答/请求）。"""

    def __init__(self, vault_manager: VaultManager):
        self._vm = vault_manager
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="distill")

    # ---------------------------------------------------------- 启动

    def start_batch(self, scope: str = "all", subject: str | None = None) -> str:
        ctx = self._vm.require()
        if not settings.get("LLM_API_KEY"):
            raise llm.LLMNotConfigured("批量初始化需要先配置 LLM_API_KEY")
        if scope not in ("all", "subject"):
            raise ValueError(f"未知 scope: {scope}")
        if scope == "subject" and subject not in ctx.subjects:
            raise ValueError(f"学科不存在: {subject}")

        have_wiki = {w.raw_rel for w in ctx.wiki_files if w.raw_rel}
        pending = [
            r.rel
            for r in ctx.raw_files
            if r.rel not in have_wiki
            and (scope == "all" or r.subject == subject)
        ]
        task = TaskRecord(
            task_id=uuid.uuid4().hex[:12],
            mode="batch", scope=scope, subject=subject,
            total=len(pending), pending=pending,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        self._submit(task)
        return task.task_id

    def start_lazy(self, raw_rel: str) -> str:
        task = TaskRecord(
            task_id=uuid.uuid4().hex[:12], mode="lazy", total=1, pending=[raw_rel]
        )
        with self._lock:
            self._tasks[task.task_id] = task
        self._submit(task)
        return task.task_id

    def _submit(self, task: TaskRecord) -> None:
        self._pool.submit(self._run, task)

    # ---------------------------------------------------------- 执行

    def _run(self, task: TaskRecord) -> None:
        log_event(
            "WIKI_TASK_START", task_id=task.task_id,
            mode=task.mode, total=task.total,
        )
        # 断点续传语义：pending 在启动时 = 尚无 Wiki 的 Raw；
        # 失败的文件记录错误后跳过，其余文件在下次批量时仍会因缺 Wiki 被拾起。
        for raw_rel in list(task.pending):
            if task.status == "finished":
                break
            task.current_file = raw_rel
            self._snapshot(task)
            try:
                distill_and_save(self._vm, raw_rel)
            except Exception as e:  # noqa: BLE001
                task.errors.append({"raw": raw_rel, "error": str(e)})
                log_event("WIKI_DISTILL_ERROR", raw=raw_rel, error=str(e))
            finally:
                task.completed += 1
                self._snapshot(task)
        task.status = "finished"
        task.current_file = None
        self._snapshot(task)
        log_event(
            "WIKI_TASK_DONE", task_id=task.task_id,
            completed=task.completed, errors=len(task.errors),
        )

    def _snapshot(self, task: TaskRecord) -> None:
        """任务进度持久化到 wiki/.llmwiki/tasks/（审计 + 重启后可查）。"""
        ctx = self._vm.get()
        if ctx is None:
            return
        try:
            d = ctx.root / WIKI_PREFIX / STATE_DIR / "tasks"
            d.mkdir(parents=True, exist_ok=True)
            rec = {**task.view(), "pending_left": len(task.pending) - task.completed,
                   "created_at": task.created_at}
            (d / f"{task.task_id}.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError:
            pass

    # ---------------------------------------------------------- 查询

    def get(self, task_id: str) -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task.view()

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return [t.view() for t in sorted(self._tasks.values(), key=lambda x: x.created_at)]
