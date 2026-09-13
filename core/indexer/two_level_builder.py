"""两级索引构建器（计划书 §13/§27 two_level_builder.py）。

- 扫描目录、解析 Markdown 标题与双链
- 生成 Master INDEX（wiki/INDEX.md）与 Sub INDEX（wiki/<学科>/INDEX.md）
- 全程普通 Python 逻辑，不调用 LLM（"0 Token" 初始化）
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

from core.graph import format_adapter as fa
from core.graph import relation_engine
from core.log import log_event
from core.vault.raw_guardian import STATE_DIR, write_wiki_file
from core.vault.vault_manager import INDEX_NAME, RAW_PREFIX, VaultContext, WIKI_PREFIX

WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def parse_wikilinks(text: str) -> list[str]:
    """提取双链 target（| 前的部分），去重保序。"""
    out: list[str] = []
    for m in WIKILINK_RE.finditer(text):
        t = m.group(1).split("|", 1)[0].strip()
        if t and t not in out:
            out.append(t)
    return out


def parse_markdown_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    title = name = path.stem
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# ") and len(s) > 2:
            title = s[2:].strip()
            break
    return {"title": title, "text": text, "links": parse_wikilinks(text)}


# ------------------------------------------------------------------ 生成

def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_master_index_markdown(ctx: VaultContext) -> str:
    subjects = ctx.subjects
    coverage = f"{ctx.wiki_count}/{ctx.raw_count}"
    lines = [
        "# Master WikiIndex",
        "",
        f"> 🤖 由 LLMwiki 自动生成（0 Token 初始化） · {_now()}",
        f"> Raw 笔记 {ctx.raw_count} 篇 · Wiki Card {ctx.wiki_count} 篇（{coverage}）",
        "",
        "## 知识模块",
        "",
    ]
    if not subjects:
        lines.append("（尚未发现任何学科目录；请在 raw/ 下建立学科子目录）")
    for s in subjects:
        raw_n = sum(1 for r in ctx.raw_files if r.subject == s)
        wiki_n = sum(
            1 for w in ctx.wiki_files if w.subject == s and not w.is_index
        )
        link = fa.build_md_link(WIKI_PREFIX, f"{WIKI_PREFIX}/{s}/{INDEX_NAME}", s)
        lines.append(f"- {link} （Wiki {wiki_n} / Raw {raw_n}）")

    root_raws = [r for r in ctx.raw_files if not r.subject]
    if root_raws:
        lines += ["", "## 根目录笔记", ""]
        for r in root_raws:
            wikified = (ctx.root / WIKI_PREFIX / f"{r.name}.md").exists()
            card = (
                fa.build_md_link(WIKI_PREFIX, f"{WIKI_PREFIX}/{r.name}.md", r.name)
                if wikified
                else r.name
            )
            raw_link = fa.build_md_link(WIKI_PREFIX, r.rel, f"{r.title} (raw)")
            if wikified:
                lines.append(f"- {card} ✅ ｜ raw：{raw_link}")
            else:
                lines.append(f"- {r.title} ⬜（未 Wiki 化）｜ raw：{raw_link}")
    lines.append("")
    return "\n".join(lines)


def build_sub_index_markdown(ctx: VaultContext, subject: str) -> str:
    raws = [r for r in ctx.raw_files if r.subject == subject]
    cards = {
        w.name: w
        for w in ctx.wiki_files
        if w.subject == subject and not w.is_index
    }
    wiki_n = len(cards)
    lines = [
        f"# {subject} · Sub WikiIndex",
        "",
        f"> 🤖 自动生成 · {_now()} · Wiki {wiki_n}/{len(raws)}",
        "",
        "## 知识点",
        "",
    ]
    if not raws:
        lines.append("（该模块下暂无 Raw 笔记）")
    from_dir = f"{WIKI_PREFIX}/{subject}"
    for r in sorted(raws, key=lambda x: x.name):
        card_link = fa.build_md_link(
            from_dir, f"{WIKI_PREFIX}/{subject}/{r.name}.md", r.name
        )
        raw_link = fa.build_md_link(from_dir, r.rel, f"{r.title} (raw)")
        if r.name in cards:
            lines.append(f"- {card_link} ✅ ｜ raw：{raw_link}")
        else:
            lines.append(f"- {r.title} ⬜（未 Wiki 化）｜ raw：{raw_link}")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ 入口

def init_indexes(vault_manager) -> dict:
    """重建 Master + 全部 Sub INDEX。不调用 LLM。"""
    ctx = vault_manager.require()
    log_event("INDEX_BUILD_START", path=str(ctx.root))

    # wiki/<学科>/ 目录（含 raw 独有而 wiki 尚无的学科）
    for s in ctx.subjects:
        (ctx.root / WIKI_PREFIX / s).mkdir(parents=True, exist_ok=True)

    for s in ctx.subjects:
        write_wiki_file(
            ctx.root,
            f"{WIKI_PREFIX}/{s}/{INDEX_NAME}",
            build_sub_index_markdown(ctx, s),
            manifest=ctx.manifest,
        )
    write_wiki_file(
        ctx.root,
        f"{WIKI_PREFIX}/{INDEX_NAME}",
        build_master_index_markdown(ctx),
        manifest=ctx.manifest,
    )

    vault_manager.refresh_wiki_only()
    relation_engine.rebuild(vault_manager)  # v2.2：索引初始化同步刷新关系拓扑
    stats = ctx.stats()
    log_event("INDEX_BUILD_COMPLETE", **stats)
    return stats


def update_indexes_for(vault_manager, subject: str | None) -> None:
    """写 Wiki 卡片后增量更新对应 Sub INDEX 与 Master INDEX。"""
    ctx = vault_manager.require()
    if subject:
        write_wiki_file(
            ctx.root,
            f"{WIKI_PREFIX}/{subject}/{INDEX_NAME}",
            build_sub_index_markdown(ctx, subject),
            manifest=ctx.manifest,
        )
    write_wiki_file(
        ctx.root,
        f"{WIKI_PREFIX}/{INDEX_NAME}",
        build_master_index_markdown(ctx),
        manifest=ctx.manifest,
    )


# ------------------------------------------------------------------ 树

def get_tree(ctx: VaultContext) -> dict:
    """Vault 真实目录树（前端「索引树」Tab 数据源，v2.1 布局无关）。

    - 按 vault 真实层级展示；standard 布局仅展示 raw/ 与 wiki/ 两棵子树（与 v2.0 一致），
      其余布局展示全库（隐藏目录/文件、*.tmp、wiki/.llmwiki/ 除外）
    - 目录节点聚合其下 .md 数量（count）；无 md 的目录不展示
    - 源笔记节点带 wikified 标记：查卡片溯源行解析出的 raw_rel 映射（比路径镜像稳）
    """
    raw_card_map = {w.raw_rel: True for w in ctx.wiki_files if w.raw_rel}
    plan = ctx.plan
    top_allowed = {RAW_PREFIX, WIKI_PREFIX} if plan.mode == "standard" else None

    def build_dir(dir_path, rel: str) -> dict | None:
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name)
        except OSError:
            return None
        dirs, files = [], []
        for p in entries:
            child_rel = f"{rel}/{p.name}" if rel else p.name
            if p.is_dir():
                if p.name == STATE_DIR or p.name.startswith("."):
                    continue
                node = build_dir(p, child_rel)
                if node and node["count"]:
                    dirs.append(node)
            elif p.name.endswith(".md") and not p.name.endswith(".tmp") and not p.name.startswith("."):
                is_wiki = child_rel == WIKI_PREFIX or child_rel.startswith(WIKI_PREFIX + "/")
                node = {
                    "name": p.name,
                    "type": "wiki" if is_wiki else "raw",
                    "rel": child_rel,
                    "count": 1,
                }
                if not is_wiki:
                    node["wikified"] = child_rel in raw_card_map
                files.append(node)
        children = dirs + files
        return {
            "name": dir_path.name or rel,
            "type": "dir",
            "rel": rel,
            "count": sum(c["count"] for c in children),
            "children": children,
        }

    root_children = []
    for p in sorted(ctx.root.iterdir(), key=lambda x: x.name):
        if p.name.startswith(".") or p.name == STATE_DIR:
            continue
        if top_allowed is not None and p.name not in top_allowed:
            continue
        if p.is_dir():
            node = build_dir(p, p.name)
            if node and node["count"]:
                root_children.append(node)
        elif p.name.endswith(".md") and not p.name.endswith(".tmp"):
            root_children.append(
                {
                    "name": p.name,
                    "type": "raw",
                    "rel": p.name,
                    "count": 1,
                    "wikified": p.name in raw_card_map,
                }
            )
    return {
        "name": ctx.name,
        "type": "root",
        "rel": "",
        "count": sum(c["count"] for c in root_children),
        "children": root_children,
    }
