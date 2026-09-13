"""Raw 安全机制（计划书 §6）。

- scan_raw_manifest: 记录 raw/ 全部文件的 path / mtime / hash
- verify_raw_intact: 检测 Raw 是否被外部改动
- write_wiki_file:   应用唯一的 Wiki 写入口（写入前后校验关联 Raw）
- 任何针对 raw/ 的写路径都会抛 RawGuardError（写入隔离）
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

STATE_DIR = ".llmwiki"  # wiki/ 下的应用元数据目录（不作为 Wiki 卡片）
RAW_PREFIX = "raw"


class RawGuardError(Exception):
    """Raw 保护或写入隔离违规。"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- manifest

def scan_raw_manifest(vault_root: Path, plan=None) -> dict[str, dict]:
    """扫描源笔记范围全部文件 → {rel_path: {mtime, hash, size}}。

    plan 为 None 时保持 v2.0 行为（扫 raw/ 子树）；否则由布局适配器给出范围
    （standard = raw/，其余 = 全库减排除项）。键始终是真实磁盘相对路径。
    """
    manifest: dict[str, dict] = {}
    if plan is None:
        raw_dir = vault_root / RAW_PREFIX
        for p in sorted(raw_dir.rglob("*")):
            if p.is_file() and not p.name.endswith(".tmp"):
                st = p.stat()
                manifest[p.relative_to(vault_root).as_posix()] = {
                    "mtime": int(st.st_mtime),
                    "hash": sha256_file(p),
                    "size": st.st_size,
                }
        return manifest
    from core.vault import layout_adapter
    for rel in layout_adapter.iter_source_files(vault_root, plan):
        p = vault_root / rel
        if p.is_file():
            st = p.stat()
            manifest[rel] = {
                "mtime": int(st.st_mtime),
                "hash": sha256_file(p),
                "size": st.st_size,
            }
    return manifest


def manifest_file(vault_root: Path) -> Path:
    return vault_root / "wiki" / STATE_DIR / "raw_manifest.json"


def load_manifest(vault_root: Path) -> dict | None:
    f = manifest_file(vault_root)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def save_manifest(vault_root: Path, manifest: dict) -> None:
    f = manifest_file(vault_root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def verify_raw_intact(vault_root: Path, manifest: dict) -> list[str]:
    """返回与清单不一致（被修改/删除）的 raw 相对路径列表。"""
    changed: list[str] = []
    for rel, meta in manifest.items():
        p = vault_root / rel
        if not p.exists() or sha256_file(p) != meta.get("hash"):
            changed.append(rel)
    return changed


# ---------------------------------------------------------------- 写入隔离

def _assert_safe_wiki_rel(vault_root: Path, wiki_rel: str) -> Path:
    """只允许写 wiki/ 下的合法路径；raw/ 与越界路径一律拒绝。"""
    if not wiki_rel.startswith("wiki/"):
        raise RawGuardError(f"[写入隔离] 拒绝写 wiki/ 以外的路径: {wiki_rel}")
    if ".." in wiki_rel:
        raise RawGuardError(f"[写入隔离] 非法路径: {wiki_rel}")
    root = vault_root.resolve()
    target = (vault_root / wiki_rel).resolve()
    if not str(target).startswith(str(root) + os.sep):
        raise RawGuardError(f"[写入隔离] 路径越界: {wiki_rel}")
    if f"/{STATE_DIR}/" in wiki_rel:
        raise RawGuardError("[写入隔离] 状态目录请使用 save_manifest 等专用接口")
    return target


def check_raw_before_write(vault_root: Path, manifest: dict, raw_rel: str | None) -> None:
    """写 Wiki 前校验关联 Raw 的完整性（事件 RAW_GUARD_CHECK）。"""
    from core.log import log_event

    if not raw_rel:
        return
    meta = manifest.get(raw_rel)
    p = vault_root / raw_rel
    if meta is None:
        if p.exists():
            raise RawGuardError(f"Raw 清单中不存在该文件但文件已出现: {raw_rel}")
        return
    if not p.exists():
        log_event("RAW_GUARD_CHECK", raw=raw_rel, result="missing")
        raise RawGuardError(f"Raw 文件已被删除: {raw_rel}")
    if sha256_file(p) != meta["hash"]:
        log_event("RAW_GUARD_CHECK", raw=raw_rel, result="modified")
        raise RawGuardError(f"Raw 文件在写入 Wiki 前已被修改: {raw_rel}")
    log_event("RAW_GUARD_CHECK", raw=raw_rel, result="ok")


def check_raw_after_write(vault_root: Path, manifest: dict, raw_rel: str | None) -> None:
    """写 Wiki 后复查 Raw 仍然一致，否则报告异常。"""
    try:
        check_raw_before_write(vault_root, manifest, raw_rel)
    except RawGuardError as e:
        raise RawGuardError(f"[Raw 保护] 写入 Wiki 后检测到 Raw 异常: {e}") from e


def write_wiki_file(
    vault_root: Path,
    wiki_rel: str,
    content: str,
    *,
    manifest: dict | None = None,
    raw_rel: str | None = None,
) -> Path:
    """应用唯一的 Wiki 写入口：tmp 文件 + 原子替换 + 前后校验。"""
    target = _assert_safe_wiki_rel(vault_root, wiki_rel)
    m = manifest if manifest is not None else (load_manifest(vault_root) or {})
    check_raw_before_write(vault_root, m, raw_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    check_raw_after_write(vault_root, m, raw_rel)
    return target
