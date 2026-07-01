"""测试脑图解析器：把 Markdown 大纲（.md）解析为与 doc_parser 同 shape 的字典。

输出契约（与 doc_parser.parse_document 保持一致，方便下游统一处理）::

    {
        "chunks": [
            {"heading": str|None, "level": int, "content": str, "type": "text"},
            ...
        ],
        "tables": [],
        "raw_text": str,           # 直接保留原 markdown 文本（带缩进），LLM 友好
        "num_paragraphs": int,     # 节点数（heading + list item 总数）
    }

为什么不复用 doc_parser：
- doc_parser 专攻产品文档（docx/pdf），结构是按段落 + 表格抽取
- 脑图是层级树（Markdown outline），节点本身就是结构化的；保留缩进文本喂 LLM 比破坏成纯文本更有效
"""
from __future__ import annotations

import re
from typing import Any


# 匹配 Markdown 列表条目：- / * / + 起头，或 "1. " 形式有序列表
_LIST_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<bullet>[-*+]|\d+[.)])\s+(?P<content>.+?)\s*$")
# 匹配 ATX heading：# / ## / ### …（最多 6 个）
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<content>.+?)\s*$")


def _decode(file_bytes: bytes) -> str:
    """优先 UTF-8，回退 GBK。脑图 .md 多为编辑器导出，这两种最常见。"""
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    # 最后兜底：忽略错误字节，确保不抛出
    return file_bytes.decode("utf-8", errors="ignore")


def parse_mindmap_md(file_bytes: bytes) -> dict[str, Any]:
    """解析 Markdown 大纲格式的测试脑图。

    支持的格式：
    - ATX 标题（# / ## / …）→ level 取 # 数量
    - 列表项（-/*/+/有序数字）→ level 由缩进推断（每 2 空格或 1 tab 视为一级）

    现实场景里大多数脑图工具（XMind / 幕布 / Logseq / Obsidian）导出的 .md 都符合上述任一形态。
    """
    raw_text = _decode(file_bytes).replace("\r\n", "\n").replace("\r", "\n")

    chunks: list[dict[str, Any]] = []
    raw_parts: list[str] = []
    current_heading: str | None = None
    current_level: int = 0
    para_count = 0

    for line in raw_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # 1) Heading 优先（必须从行首开始，不允许缩进）
        m_h = _HEADING_RE.match(line)
        if m_h:
            level = len(m_h.group("hashes"))
            content = m_h.group("content").strip()
            current_heading = content
            current_level = level
            chunks.append({
                "heading": content,
                "level": level,
                "content": content,
                "type": "text",
            })
            raw_parts.append(f"{'#' * min(level, 6)} {content}")
            para_count += 1
            continue

        # 2) 列表项（用缩进推 level，2 空格 / 1 tab 算一级；最少 1 级）
        m_l = _LIST_RE.match(line)
        if m_l:
            indent = m_l.group("indent") or ""
            # 把 tab 当作 4 个空格，与多数编辑器渲染一致
            indent_cols = indent.replace("\t", "    ")
            depth = len(indent_cols) // 2 + 1
            content = m_l.group("content").strip()
            chunks.append({
                "heading": current_heading,
                "level": depth,
                "content": content,
                "type": "text",
            })
            # 保留原始缩进与符号，让 LLM 能直观看到层级
            raw_parts.append(f"{indent}{m_l.group('bullet')} {content}")
            para_count += 1
            continue

        # 3) 普通段落（兜底）：当作平级文本
        chunks.append({
            "heading": current_heading,
            "level": current_level or 1,
            "content": stripped,
            "type": "text",
        })
        raw_parts.append(stripped)
        para_count += 1

    return {
        "chunks": chunks,
        "tables": [],
        "raw_text": "\n".join(raw_parts),
        "num_paragraphs": para_count,
    }
