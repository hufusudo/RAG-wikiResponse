"""结构化事件日志（计划书 §29 原则 6）。

用法: log_event("VAULT_OPEN", path=..., raw_count=...)
输出: 2026-.. | INFO | llmwiki | VAULT_OPEN {"path": "..."}
"""
from __future__ import annotations

import json
import logging
import sys

_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _configured = True


def log_event(event: str, **fields) -> None:
    logging.getLogger("llmwiki").info(
        "%s %s", event, json.dumps(fields, ensure_ascii=False, default=str)
    )
