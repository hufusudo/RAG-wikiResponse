"""链接与溯源格式适配器（计划书 v2.2 §2 + 评审注 1/3）。

溯源行是 wiki↔raw 关联的**唯一权威**。v2.2 起默认输出标准 Markdown 相对链接，
同时**永久兼容**旧 Obsidian 双链格式（存量卡片无需迁移，读取侧双格式解析）：

  标准（默认）  : > 📖 原始笔记：[线程 (raw)](<../../raw/操作系统/线程.md>)
  Obsidian 兼容 : > 📖 原始笔记：[[raw/操作系统/线程.md|线程 (raw)]]

路径语义（评审注 3）：
  - 新格式：目标 = 「卡片所在目录 → Raw」的相对路径（os.path.relpath 语义）；
    含空格/括号等字符时用尖括号包裹（CommonMark / GitHub / VS Code / Typora 原生支持）。
  - 旧格式：目标 = 仓库根相对路径（v2.1 语义）。
  两种格式经 parse_raw_rel 统一解析为「仓库根相对路径」。

本模块是全系统唯一的溯源行解析点（v2.1 中散落 4 处的正则在此收口，
消除「漏改一处即回归」的风险）。仅依赖标准库，可被 vault/graph/rag 各层安全引用。
"""
from __future__ import annotations

import os
import re
import posixpath

TRACE_PREFIX = "> 📖 原始笔记"  # 不含冒号；构造时统一补全角冒号

# 旧格式（v2.1 Obsidian 双链）：目标 = 仓库根相对路径
OBSIDIAN_TRACE_RE = re.compile(
    r"^>\s*📖\s*原始笔记：\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]\s*$", re.M
)
# 新格式（v2.2 标准 Markdown）：目标 = 卡片目录相对路径；尖括号可选
MD_TRACE_RE = re.compile(
    r"^>\s*📖\s*原始笔记：\[[^\]]*\]\((?:<([^>]+)>|([^)\s]+))\)\s*$", re.M
)
# 保留区守卫用（layout_adapter）：只判断「是否系统生成的溯源行」，不提取目标
TRACE_HINT_RE = re.compile(r"^>\s*📖\s*原始笔记：\[", re.M)

WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\((?:<([^>]+)>|([^)\s]+))\)")

# 标准链接目标需要尖括号包裹的字符（CommonMark：空白/括号会破坏内联链接）
_NEEDS_ANGLE_RE = re.compile(r"[\s()]")


# ---------------------------------------------------------------- 溯源行解析

def is_trace_line(line: str) -> bool:
    """该行是否为溯源行（两种格式通用；用于正文链接抽取时跳过、悬空降级时豁免）。"""
    return bool(TRACE_HINT_RE.match(line))


def strip_trace(text: str) -> str:
    """去掉正文中的第一行溯源行（任意格式），返回剩余文本。"""
    text = OBSIDIAN_TRACE_RE.sub("", text, count=1)
    text = MD_TRACE_RE.sub("", text, count=1)
    return text.strip()


def parse_raw_rel(text: str, card_rel: str) -> str | None:
    """从卡片文本解析溯源目标，统一返回**仓库根相对路径**；无溯源行返回 None。

    - 旧格式目标即仓库根相对路径，原样返回
    - 新格式目标相对卡片所在目录，先换算回仓库根相对路径
    """
    m = OBSIDIAN_TRACE_RE.search(text)
    if m:
        target = m.group(1).strip()
        return target or None
    m = MD_TRACE_RE.search(text)
    if m:
        target = (m.group(1) or m.group(2)).strip()
        if not target:
            return None
        return resolve_relpath(card_rel, target)
    return None


def resolve_relpath(card_rel: str, target: str) -> str:
    """「卡片 rel + 卡片目录相对目标」→ 规范化的仓库根相对路径。"""
    base = posixpath.dirname(card_rel)
    joined = posixpath.join(base, target) if base else target
    return posixpath.normpath(joined)


# ---------------------------------------------------------------- 溯源行构造

def build_md_link(from_dir_rel: str, to_rel: str, label: str) -> str:
    """标准 Markdown 相对链接 [label](target)。

    from_dir_rel/to_rel 均为仓库根相对路径；target = from_dir → to_rel 的相对路径，
    含空格/括号时自动尖括号包裹。
    """
    target = _md_target(from_dir_rel, to_rel)
    return f"[{label}]({target})"


def build_trace_line(card_rel: str, raw_rel: str, title: str, obsidian: bool = False) -> str:
    """构造溯源行。card_rel/raw_rel 均为仓库根相对路径。

    - 标准（默认）：[标题 (raw)](<卡片目录→Raw 相对路径>)
    - Obsidian 兼容：[[仓库根相对路径|标题 (raw)]]
    """
    if obsidian:
        return f"{TRACE_PREFIX}：[[{raw_rel}|{title} (raw)]]"
    label = f"{title} (raw)"
    target = _md_target(posixpath.dirname(card_rel), raw_rel)
    return f"{TRACE_PREFIX}：[{label}]({target})"


def _md_target(from_dir_rel: str, to_rel: str) -> str:
    """仓库根相对路径 → from_dir 目录视角的 Markdown 链接目标（posix，必要时尖括号）。"""
    target = os.path.relpath(to_rel, from_dir_rel or ".").replace(os.sep, "/")
    if _NEEDS_ANGLE_RE.search(target):
        target = f"<{target}>"
    return target


# ---------------------------------------------------------------- 正文链接抽取

def extract_body_links(text: str) -> list[str]:
    """抽取正文链接目标（多源关系抽取的输入，v2.2 §1.1）。

    - 标准 Markdown 链接 [text](target) 与 [[双链]] 均抽取（Markdown 优先顺序不敏感，
      关系引擎负责解析与去重）
    - 溯源行豁免（它指向 Raw 层而非 Wiki 概念层）
    - 返回「按书写形式」的目标串，顺序保序、不去重（调用方决定去重策略）
    """
    out: list[str] = []
    for line in text.splitlines():
        if is_trace_line(line):
            continue
        for m in WIKILINK_RE.finditer(line):
            t = m.group(1).split("|", 1)[0].strip()
            if t:
                out.append(t)
        for m in MD_LINK_RE.finditer(line):
            t = (m.group(1) or m.group(2)).strip()
            if t and not t.startswith(("#", "http://", "https://", "mailto:")):
                out.append(t)
    return out
