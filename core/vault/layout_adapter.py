"""布局适配层（计划书 v2.1 §2 / 布局适配设计方案.md §4）。

打开任意组织的 Markdown 笔记仓库时，检测布局并产出 LayoutPlan：
- 源笔记扫描范围（standard = raw/ 子树；其余 = 全库减排除项）
- 学科映射（学科目录真实相对路径 → 显示名）
- 排除规则（硬编码默认 + layout.json 用户自定义，按路径段名匹配）
- warnings（非致命提示，随打开结果返回前端）

设计哲学：纯文件系统逻辑（0 Token）；绝不移动/重命名用户文件；
检测歧义与保留区冲突 → 结构化报错（LayoutError，由 vault_manager 转为 VaultError）。

分层优先级（设计方案 §4.5）：
  学科归属：手动声明 > frontmatter subject: > 目录派生 > 单一伪学科
  排除规则：layout.json excludes > 硬编码默认（隐藏项 / 根级 wiki/ / *.tmp）

检测规则（首中即停）：
  R1 standard    顶层有 raw/ → 源笔记 = raw/ 子树，raw/ 下一级含 md 目录 = 学科
  R2 per_subject ≥2 个顶层目录含 md → 每个含 md 顶层目录 = 学科
  R3 flat        仅 1 个含 md 顶层目录或 md 全在根级 → 单一伪学科/无学科
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.graph.format_adapter import TRACE_HINT_RE as _TRACE_HINT

WIKI_PREFIX = "wiki"  # 根级保留写入区（与 vault_manager.WIKI_PREFIX 同值；避免循环导入）
INDEX_BASENAME = "INDEX.md"
RAW_PREFIX = "raw"
LAYOUT_FILE = "layout.json"  # 位于 wiki/.llmwiki/ 下
# 卡片溯源行识别（v2.2 收口至 core.graph.format_adapter；双格式兼容，仅判断是否系统生成）
_DISPLAY_SUFFIXES = ("wiki", "Wiki", "笔记", "notes", "Notes")


class LayoutError(Exception):
    """布局检测失败/保留区冲突（结构化信息返回给前端）。"""


@dataclass
class LayoutPlan:
    mode: str  # "standard" | "per_subject" | "flat" | "manual"
    subjects: dict[str, str] = field(default_factory=dict)  # 学科目录 rel → 显示名
    scope: str = "."  # 源笔记扫描根："raw" 或 "."
    warnings: list[str] = field(default_factory=list)
    manual: bool = False  # 来自 layout.json / 请求参数的手动声明
    excludes: list[str] = field(default_factory=list)  # 用户自定义排除（按路径段名匹配）

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "subjects": dict(self.subjects),
            "scope": self.scope,
            "warnings": list(self.warnings),
            "manual": self.manual,
            "excludes": list(self.excludes),
        }


# ---------------------------------------------------------------- 显示名

def display_name(dir_name: str) -> str:
    """学科显示名：去掉一个尾部约定后缀（wiki/笔记/notes…）。"""
    for suf in _DISPLAY_SUFFIXES:
        if dir_name.endswith(suf) and len(dir_name) > len(suf):
            return dir_name[: -len(suf)]
    return dir_name


def _dedupe_display(subjects: dict[str, str], warnings: list[str]) -> dict[str, str]:
    """显示名撞名（如 数学wiki 与 数学笔记）→ 全部保留目录原名。"""
    seen: dict[str, str] = {}
    collision = False
    for d, name in subjects.items():
        if name in seen:
            collision = True
            break
        seen[name] = d
    if collision:
        warnings.append("学科显示名冲突（去 wiki 后缀后撞名），全部保留目录原名")
        return {d: Path(d).name for d in subjects}
    return subjects


# ---------------------------------------------------------------- 排除与保留区

def is_hidden(name: str) -> bool:
    return name.startswith(".")


def _is_excluded_parts(parts: list[str], excludes: list[str]) -> bool:
    """用户自定义排除：任一路径段命中即排除（按段名匹配，简单可预期）。"""
    return any(p in excludes for p in parts)


def _assert_no_user_wiki(root: Path) -> None:
    """根级 wiki/ 是系统保留写入区；含用户自己的 md 时拒绝打开（绝不写用户文件）。"""
    wiki = root / WIKI_PREFIX
    if not wiki.is_dir():
        return
    offenders: list[str] = []
    for p in sorted(wiki.rglob("*.md")):
        rel_parts = p.relative_to(root).parts
        if any(is_hidden(x) for x in rel_parts):
            continue
        if p.name == INDEX_BASENAME:
            continue  # 系统生成的索引
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:600]
        except OSError:
            head = ""
        if _TRACE_HINT.search(head):
            continue  # 系统生成的卡片（头部有溯源行）
        offenders.append(p.relative_to(root).as_posix())
    if offenders:
        raise LayoutError(
            f"根级 wiki/ 为系统保留写入区，但发现非系统生成的 Markdown（{len(offenders)} 个，"
            f"如 {offenders[0]}）。请重命名该目录后重试；系统不会移动用户文件。"
        )


# ---------------------------------------------------------------- 检测

def _top_dirs_with_md(root: Path) -> list[str]:
    out = []
    for p in sorted(root.iterdir()):
        if not p.is_dir() or is_hidden(p.name) or p.name == WIKI_PREFIX:
            continue
        if any(q.suffix == ".md" for q in p.rglob("*.md")):
            out.append(p.name)
    return out


def _detect_standard(root: Path, warnings: list[str]) -> LayoutPlan:
    raw = root / RAW_PREFIX
    subjects = {
        f"{RAW_PREFIX}/{p.name}": display_name(p.name)
        for p in sorted(raw.iterdir())
        if p.is_dir() and not is_hidden(p.name)
        and any(q.suffix == ".md" for q in p.rglob("*.md"))
    }
    subjects = _dedupe_display(subjects, warnings)
    # raw/ 之外散落的 md → 提示不纳入（R1 优先，保持 v2.0 行为）
    outside = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.md")
        if not p.relative_to(root).parts[0].startswith(RAW_PREFIX)
        and not is_hidden(p.relative_to(root).parts[0])
        and p.relative_to(root).parts[0] != WIKI_PREFIX
    ]
    if outside:
        warnings.append(
            f"raw/ 外发现 {len(outside)} 个 Markdown 文件未纳入源笔记（如 {outside[0]}）；"
            "如需纳入，请在打开时手动指定学科"
        )
    return LayoutPlan(mode="standard", subjects=subjects, scope=RAW_PREFIX, warnings=warnings)


def _detect_flat(root: Path, dirs: list[str], warnings: list[str]) -> LayoutPlan:
    subjects = {dirs[0]: display_name(dirs[0])} if dirs else {}
    subjects = _dedupe_display(subjects, warnings)
    root_md = [p.name for p in root.iterdir() if p.is_file() and p.suffix == ".md"]
    if dirs and root_md:
        warnings.append(f"根级 {len(root_md)} 篇笔记不归属任何学科，将作为无学科笔记处理")
    return LayoutPlan(mode="flat", subjects=subjects, scope=".", warnings=warnings)


def detect(root: Path) -> LayoutPlan:
    """自动检测布局（R1 → R2 → R3，首中即停）。仓库无任何 md 时报错。"""
    _assert_no_user_wiki(root)
    if (root / RAW_PREFIX).is_dir():
        return _detect_standard(root, [])
    dirs = _top_dirs_with_md(root)
    if len(dirs) >= 2:
        ws: list[str] = []
        subjects = _dedupe_display({d: display_name(d) for d in dirs}, ws)
        return LayoutPlan(mode="per_subject", subjects=subjects, scope=".", warnings=ws)
    return _detect_flat(root, dirs, [])


# ---------------------------------------------------------------- 手动声明

def _load_layout_json(root: Path) -> dict | None:
    f = root / WIKI_PREFIX / ".llmwiki" / LAYOUT_FILE
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("mode") == "manual" else None
    except Exception:  # noqa: BLE001 — 布局文件损坏按未配置处理
        return None


def _build_manual(root: Path, decl: dict, warnings: list[str]) -> LayoutPlan | None:
    """手动声明：{"mode":"manual","subjects":{目录rel:显示名},"excludes":[段名]}。
    目录不存在则整体回落自动检测（warnings 说明）。"""
    raw_subjects = decl.get("subjects")
    if not isinstance(raw_subjects, dict) or not raw_subjects:
        return None
    subjects: dict[str, str] = {}
    for d, name in raw_subjects.items():
        if not (root / d).is_dir():
            warnings.append(f"手动声明的学科目录不存在，已忽略：{d}")
            continue
        subjects[d] = name or display_name(Path(d).name)
    if not subjects:
        return None
    subjects = _dedupe_display(subjects, warnings)
    excludes = [str(x) for x in decl.get("excludes", []) if str(x)]
    return LayoutPlan(
        mode="manual",
        subjects=subjects,
        scope=".",
        warnings=warnings,
        manual=True,
        excludes=excludes,
    )


def resolve(root: Path, override: dict | None = None) -> LayoutPlan:
    """打开仓库时的布局决策入口。

    优先级：请求参数 override > wiki/.llmwiki/layout.json > 自动检测。
    手动声明中目录失效时回落自动检测；excludes 随 plan 一并生效。
    """
    warnings: list[str] = []
    decl = override if isinstance(override, dict) and override.get("subjects") else _load_layout_json(root)
    if decl is not None:
        plan = _build_manual(root, decl, warnings)
        if plan is not None:
            _assert_no_user_wiki(root)
            if override is not None:
                plan.warnings.append("本次打开使用了请求参数中的手动布局声明（未写入 layout.json）")
            _assert_has_md(root, plan)
            return plan
        warnings.append("手动布局声明无效，已回落自动检测")
    plan = detect(root)
    plan.warnings = warnings + plan.warnings
    _assert_has_md(root, plan)
    return plan


def _assert_has_md(root: Path, plan: LayoutPlan) -> None:
    """应用排除规则后源笔记范围内无任何 Markdown → 结构化报错（设计方案 §4.3）。"""
    if not any(rel.endswith(".md") for rel in iter_source_files(root, plan)):
        raise LayoutError(
            "未发现任何 Markdown 笔记（已应用排除规则）；"
            "请确认目录是否为笔记仓库，或在打开时调整排除项"
        )


# ---------------------------------------------------------------- 扫描范围与学科归类

def iter_source_files(root: Path, plan: LayoutPlan) -> list[str]:
    """源笔记范围内全部文件的相对路径（posix，排序）。

    scope=raw：与 v2.0 行为字节级一致（raw/ 全部文件，仅跳过 *.tmp）。
    scope=.:   全库减排除项（隐藏段 / *.tmp / 根级 wiki/ / layout.json excludes）。
    """
    base = root / plan.scope if plan.scope != "." else root
    prefix = () if plan.scope == "." else (plan.scope,)  # 返回路径始终相对仓库根
    excludes = plan.excludes or []

    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = Path(dirpath).relative_to(base)
        if plan.scope == ".":
            dirnames[:] = sorted(
                d for d in dirnames
                if not is_hidden(d) and d not in excludes
                and not (rel_dir == Path(".") and d == WIKI_PREFIX)
            )
        else:
            dirnames[:] = sorted(dirnames)
        for fn in sorted(filenames):
            if fn.endswith(".tmp") or is_hidden(fn):
                continue
            rel_parts = prefix + rel_dir.parts + (fn,)
            if plan.scope == "." and _is_excluded_parts(list(rel_parts), excludes):
                continue
            out.append("/".join(rel_parts))
    return out


def classify(rel: str, plan: LayoutPlan) -> tuple[str, str]:
    """相对路径 → (学科显示名, subject_dir)。

    subject_dir 是"卡片路径锚点"：卡片 = wiki/<subject>/<rel 去掉 subject_dir 前缀>。
    subject_dir 为 "" 表示无学科笔记（卡片 = wiki/<rel>）。
    """
    parts = rel.split("/")
    if plan.mode == "standard":
        if parts[0] == RAW_PREFIX and len(parts) > 2:
            # 显示名与 R1 检测一致（无后缀时恒等，与 v2.0 字节级兼容）
            subject = plan.subjects.get(f"{RAW_PREFIX}/{parts[1]}", parts[1])
            return subject, f"{RAW_PREFIX}/{parts[1]}"
        if parts[0] == RAW_PREFIX:
            return "", RAW_PREFIX
        return "", ""
    top = parts[0]
    if top in plan.subjects:
        return plan.subjects[top], top
    return "", ""


# ---------------------------------------------------------------- frontmatter（分层第 2 级）

_FM_SUBJECT_RE = re.compile(r"^subject:\s*(.+?)\s*$")


def parse_frontmatter_subject(text: str) -> str | None:
    """从笔记 frontmatter 提取 subject 单键（限前 20 行，不引入 YAML 依赖）。

    优先级高于目录派生（设计方案 §4.5 第 2 层）；无 frontmatter 或无该键返回 None。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:21]:
        s = line.strip()
        if s == "---":
            break
        m = _FM_SUBJECT_RE.match(s)
        if m:
            return m.group(1).strip("'\"") or None
    return None
