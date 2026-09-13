"""Vault 管理（计划书 §12/§27 vault_manager.py）。

职责：Vault 打开 / 切换 / 目录扫描 / 路径校验 / 当前 Vault 状态。
不硬编码任何 Vault 路径；路径由用户通过 /api/vault/open 提供。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.graph import format_adapter as fa
from core.log import log_event
from core.vault import layout_adapter as la
from core.vault.layout_adapter import LayoutPlan
from core.vault.raw_guardian import (
    load_manifest,
    save_manifest,
    scan_raw_manifest,
    verify_raw_intact,
)

INDEX_NAME = "INDEX.md"
RAW_PREFIX = "raw"
WIKI_PREFIX = "wiki"


class VaultError(Exception):
    """Vault 相关错误（结构化信息返回给前端）。"""


def _first_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# ") and len(s) > 2:
            return s[2:].strip()
    return fallback


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


# 溯源行解析已收口至 core.graph.format_adapter（v2.2：双格式唯一解析点）


@dataclass
class RawFile:
    rel: str  # 真实磁盘相对路径（任意布局；standard 下为 raw/学科/知识点.md）
    subject: str  # 学科显示名；无学科笔记为 ""
    name: str  # 文件名 stem
    title: str
    mtime: int
    hash: str
    subject_dir: str = ""  # 卡片路径锚点：卡片 = wiki/<subject>/<rel 去掉该前缀>


@dataclass
class WikiFile:
    rel: str  # wiki/.../*.md
    subject: str
    name: str
    title: str
    is_index: bool
    raw_rel: str | None  # 对应 raw 文件（同相对路径）


@dataclass
class VaultContext:
    root: Path
    name: str
    opened_at: float
    plan: LayoutPlan = field(default_factory=lambda: LayoutPlan(mode="flat"))
    manifest: dict = field(default_factory=dict)
    raw_files: list[RawFile] = field(default_factory=list)
    wiki_files: list[WikiFile] = field(default_factory=list)
    external_changes: list[str] = field(default_factory=list)

    @property
    def raw_count(self) -> int:
        return len(self.raw_files)

    @property
    def wiki_count(self) -> int:
        return sum(1 for w in self.wiki_files if not w.is_index)

    @property
    def subjects(self) -> list[str]:
        seen: list[str] = []
        for r in self.raw_files:
            if r.subject and r.subject not in seen:
                seen.append(r.subject)
        for w in self.wiki_files:
            if w.subject and w.subject not in seen:
                seen.append(w.subject)
        return seen

    @property
    def index_ready(self) -> bool:
        return (self.root / WIKI_PREFIX / INDEX_NAME).exists()

    def stats(self) -> dict:
        return {
            "raw_count": self.raw_count,
            "wiki_count": self.wiki_count,
            "subjects": len(self.subjects),
            "index_ready": self.index_ready,
        }

    def raw_by_rel(self) -> dict[str, RawFile]:
        return {r.rel: r for r in self.raw_files}

    def wiki_rels(self) -> set[str]:
        return {w.rel for w in self.wiki_files}


class VaultManager:
    """全局唯一的 Vault 运行上下文持有者。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ctx: VaultContext | None = None

    # ------------------------------------------------------------- open

    def open(self, path_str: str, layout_override: dict | None = None) -> VaultContext:
        path = Path(path_str).expanduser()
        if not path.exists():
            raise VaultError(f"目录不存在: {path}")
        if not path.is_dir():
            raise VaultError(f"路径不是目录: {path}")

        with self._lock:
            t0 = time.time()
            # 布局适配（v2.1）：不再要求 raw/；检测/手动声明失败 → 结构化报错
            try:
                plan = la.resolve(path, layout_override)
            except la.LayoutError as e:
                raise VaultError(str(e)) from e
            manifest = scan_raw_manifest(path, plan)
            # 与上次持久化清单对比，识别用户/外部程序的改动（只提示，不阻止）
            old = load_manifest(path)
            external = verify_raw_intact(path, old) if old else []

            (path / WIKI_PREFIX).mkdir(exist_ok=True)
            ctx = VaultContext(
                root=path.resolve(),
                name=path.name,
                opened_at=time.time(),
                plan=plan,
                manifest=manifest,
                raw_files=self._scan_raw(path, manifest, plan),
                wiki_files=self._scan_wiki(path),
                external_changes=external,
            )
            save_manifest(path, manifest)
            self._ctx = ctx
            log_event(
                "VAULT_OPEN",
                path=str(ctx.root),
                raw_count=ctx.raw_count,
                wiki_count=ctx.wiki_count,
                ms=int((time.time() - t0) * 1000),
            )
            return ctx

    def close(self) -> None:
        with self._lock:
            self._ctx = None

    def get(self) -> VaultContext | None:
        with self._lock:
            return self._ctx

    def require(self) -> VaultContext:
        ctx = self.get()
        if ctx is None:
            raise VaultError("尚未打开 Vault，请先选择本地仓库")
        return ctx

    # ----------------------------------------------------------- refresh

    def refresh(self) -> None:
        """全量重扫（Raw + Wiki + manifest），用于索引初始化后。"""
        with self._lock:
            ctx = self._ctx
            if ctx is None:
                return
            ctx.manifest = scan_raw_manifest(ctx.root, ctx.plan)
            ctx.raw_files = self._scan_raw(ctx.root, ctx.manifest, ctx.plan)
            ctx.wiki_files = self._scan_wiki(ctx.root)
            save_manifest(ctx.root, ctx.manifest)
            log_event(
                "VAULT_REFRESH",
                raw_count=ctx.raw_count,
                wiki_count=ctx.wiki_count,
            )

    def refresh_wiki_only(self) -> None:
        """轻量刷新：只重扫 wiki/（Raw 清单保持不变，避免大 Vault 频繁全量哈希）。"""
        with self._lock:
            ctx = self._ctx
            if ctx is None:
                return
            ctx.wiki_files = self._scan_wiki(ctx.root)

    # ------------------------------------------------------------- scans

    @staticmethod
    def _scan_raw(root: Path, manifest: dict, plan: LayoutPlan) -> list[RawFile]:
        """manifest → RawFile 列表（仅 .md；学科 = 手动/frontmatter/目录派生 分层合成）。"""
        raws: list[RawFile] = []
        for rel, meta in sorted(manifest.items()):
            name = Path(rel).stem
            if name == INDEX_NAME[:-3]:  # 名为 INDEX.md 视为索引元数据
                continue
            if not rel.endswith(".md"):  # 图片等资源进 manifest（防篡改）但不作为笔记
                continue
            subject, subject_dir = la.classify(rel, plan)
            text = _read_text(root / rel)
            fm = la.parse_frontmatter_subject(text)  # 分层第 2 级：frontmatter 覆盖目录派生
            if fm:
                subject = fm
            title = _first_title(text, name)
            raws.append(
                RawFile(
                    rel=rel,
                    subject=subject,
                    name=name,
                    title=title,
                    mtime=meta["mtime"],
                    hash=meta["hash"],
                    subject_dir=subject_dir,
                )
            )
        return raws

    @staticmethod
    def _scan_wiki(root: Path) -> list[WikiFile]:
        """扫描 wiki/；raw_rel 从卡片溯源行解析（v2.1：不再路径镜像推断）。"""
        wikis: list[WikiFile] = []
        wiki_dir = root / WIKI_PREFIX
        if not wiki_dir.is_dir():
            return wikis
        for p in sorted(wiki_dir.rglob("*.md")):
            rel = p.relative_to(root).as_posix()
            if f"/{'.llmwiki'}/" in rel or rel.endswith(".tmp"):
                continue
            parts = rel.split("/")  # wiki/<subject>/<name>.md 或 wiki/<name>.md
            subject = parts[1] if len(parts) > 2 else ""
            name = p.stem
            is_index = name == INDEX_NAME[:-3]
            raw_rel = None
            if not is_index:
                text = _read_text(p)
                raw_rel = fa.parse_raw_rel(text, card_rel=rel)  # 双格式统一解析
                title = _first_title(text, name)
            else:
                title = name
            wikis.append(
                WikiFile(
                    rel=rel, subject=subject, name=name, title=title,
                    is_index=is_index, raw_rel=raw_rel,
                )
            )
        return wikis
