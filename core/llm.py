"""统一 LLM 客户端（OpenAI 兼容接口，计划书 §10）。

运行时每次调用都读取 settings，因此修改 API Key / Base URL / Model 后
无需重启即可生效（Test 7）。
"""
from __future__ import annotations

import time

from openai import OpenAI

from config.settings import settings
from core.log import log_event

DEFAULT_MODEL = "deepseek-chat"


class LLMNotConfigured(Exception):
    """LLM_API_KEY 未配置。"""


class LLMError(Exception):
    """LLM 调用失败。"""


def _client() -> OpenAI:
    api_key = settings.get("LLM_API_KEY")
    if not api_key:
        raise LLMNotConfigured("未配置 LLM_API_KEY，请先在「模型与 API Key 设置」中填写")
    return OpenAI(api_key=api_key, base_url=settings.get("LLM_BASE_URL"))


def get_model() -> str:
    return settings.get("LLM_MODEL") or DEFAULT_MODEL


def list_models() -> list[str]:
    """拉取 OpenAI 兼容 /models 列表（保存 Key/BaseURL 后自动获取模型名）。"""
    try:
        models = sorted(m.id for m in _client().models.list())
    except LLMNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001
        log_event("LLM_MODELS_FAIL", error=str(e))
        raise LLMError(f"获取模型列表失败: {e}") from e
    log_event("LLM_MODELS_OK", count=len(models))
    return models


def chat_complete(messages: list[dict], temperature: float = 0.3, max_tokens: int = 2048) -> str:
    model = get_model()
    t0 = time.time()
    try:
        resp = _client().chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
        )
    except LLMNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001
        log_event("LLM_ERROR", model=model, error=str(e))
        raise LLMError(f"LLM 调用失败: {e}") from e
    log_event(
        "LLM_REQUEST",
        model=model,
        ms=int((time.time() - t0) * 1000),
        tokens=getattr(resp.usage, "total_tokens", None),
    )
    return resp.choices[0].message.content or ""


def chat_stream(messages: list[dict], temperature: float = 0.3, max_tokens: int = 4096):
    """流式输出 token 增量（Test 8：SSE 持续接收 token）。"""
    model = get_model()
    try:
        stream = _client().chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
    except LLMNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001
        log_event("LLM_ERROR", model=model, error=str(e))
        raise LLMError(f"LLM 连接失败: {e}") from e
    for chunk in stream:
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content


def test_connection() -> dict:
    """测试当前模型配置能否连通（计划书 §11）。"""
    t0 = time.time()
    try:
        _client().chat.completions.create(
            model=get_model(),
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
    except Exception as e:  # noqa: BLE001
        log_event("LLM_TEST_FAIL", error=str(e))
        return {"success": False, "error": str(e)}
    ms = int((time.time() - t0) * 1000)
    log_event("LLM_TEST_OK", ms=ms)
    return {"success": True, "latency_ms": ms, "message": "connection ok"}
