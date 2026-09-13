"""数据飞轮（计划书 §9 模式 A / §27 flywheel_agent.py）。

问答结束后：检测命中的 Raw 是否缺少 Wiki Card → 缺少则创建后台 Lazy 精炼任务。
不阻塞当前问答（任务在线程池执行，进度可经 /api/wiki/task/{id} 查询）。
"""
from __future__ import annotations

from config.settings import settings
from core.indexer.auto_wiki_ai import DistillTaskManager
from core.log import log_event
from core.vault.vault_manager import VaultContext


class FlywheelAgent:
    def __init__(self, tasks: DistillTaskManager):
        self._tasks = tasks

    def after_chat(self, ctx: VaultContext, raw_rels: list[str]) -> dict | None:
        """返回 {'task_id', 'raw'} 或 None（无需生成/未配置 LLM）。"""
        if not raw_rels or not settings.get("LLM_API_KEY"):
            return None
        have = {w.raw_rel for w in ctx.wiki_files if w.raw_rel}
        missing = [r for r in raw_rels if r not in have]
        if not missing:
            return None
        task_id = self._tasks.start_lazy(missing[0])
        log_event("FLYWHEEL_LAZY_START", task_id=task_id, raw=missing[0])
        return {"task_id": task_id, "raw": missing}
