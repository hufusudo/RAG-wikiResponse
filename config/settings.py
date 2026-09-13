"""应用配置：.env 持久化 + 内存热更新（Test 7：改配置无需重启后端）。

配置项（计划书 §10/§11）：
    LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
    EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL
    VAULT_PATH（记住上次打开的 Vault）
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SETTINGS_KEYS = [
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "VAULT_PATH",
    "OBSIDIAN_COMPATIBILITY",  # v2.2：溯源行输出旧 Obsidian 双链格式（默认标准 Markdown）
]

SECRET_KEYS = {"LLM_API_KEY", "EMBEDDING_API_KEY"}


class AppSettings:
    """线程安全的运行时配置。读：内存；写：内存 + .env 落盘。"""

    def __init__(self, env_file: Path | None = None):
        # 测试可通过 LLMWIKI_ENV_FILE 重定向 .env 位置
        self.env_file = env_file or Path(
            os.environ.get("LLMWIKI_ENV_FILE", str(PROJECT_ROOT / ".env"))
        )
        self._lock = threading.RLock()
        self._data: dict[str, str | None] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            file_vals = dotenv_values(self.env_file) if self.env_file.exists() else {}
            # .env 文件优先（API 写入的配置必须立即生效），进程环境变量作 fallback
            self._data = {
                k: file_vals.get(k) or os.environ.get(k) for k in SETTINGS_KEYS
            }

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            v = self._data.get(key)
            return v if v not in (None, "") else default

    def is_set(self, *keys: str) -> bool:
        return all(self.get(k) for k in keys)

    def as_dict(self, mask_secrets: bool = True) -> dict[str, str | None]:
        with self._lock:
            out: dict[str, str | None] = {}
            for k in SETTINGS_KEYS:
                v = self._data.get(k)
                if mask_secrets and k in SECRET_KEYS and v:
                    out[k] = (v[:4] + "****") if len(v) > 4 else "****"
                else:
                    out[k] = v
            return out

    def update(self, values: dict) -> dict[str, str | None]:
        """更新内存配置并写回 .env；空字符串 = 清除该配置。"""
        with self._lock:
            unknown = [k for k in values if k not in SETTINGS_KEYS]
            if unknown:
                raise ValueError(f"未知配置项: {unknown}")
            for k, v in values.items():
                self._data[k] = str(v).strip() if v is not None else None
            self._persist()
            return self.as_dict()

    def _persist(self) -> None:
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        old = dotenv_values(self.env_file) if self.env_file.exists() else {}
        lines = [
            f'{k}="{v}"' for k, v in old.items() if k not in SETTINGS_KEYS and v
        ]
        for k in SETTINGS_KEYS:
            v = self._data.get(k)
            if v:
                lines.append(f'{k}="{v}"')
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


settings = AppSettings()
