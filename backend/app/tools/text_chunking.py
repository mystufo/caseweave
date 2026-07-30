"""把长文本切成适合 embedding 的窗口。

背景：知识检索原本把整篇文档（2000 字截断）压成**一个**向量去和知识库里的
**单条原子语句**做余弦近邻——长文向量是几十个主题的质心，单句是一个点，
两者余弦天然偏大（相关度偏低），语义再相关也压不下去。

解法：把文档切成句/段级 chunk 后分别 embed，检索时对每个 chunk 各跑一次近邻、
按条目取「跨所有 chunk 的最小距离」（max-sim）。这样一条知识只要和文档里
**某一段**高度相似即可命中，不再被全文其它无关内容稀释。

设计要点：
- 按段落/换行边界聚合，尽量不切碎句子；单段超长才硬切。
- 有 overlap，避免关键句正好落在窗口边界被劈开。
- 纯函数、无 IO、无异常路径——切不出来就返回 [整段]，绝不影响调用方。
"""
from __future__ import annotations

import re


def split_for_embedding(
    text: str,
    *,
    size: int = 256,
    overlap: int = 48,
) -> list[str]:
    """把 text 切成不超过 size 字符的窗口列表（按段落/句子边界聚合，带 overlap）。

    - size：单个窗口的目标最大字符数
    - overlap：相邻窗口的重叠字符数（0 ≤ overlap < size），让跨边界的关键句仍能整段落进某个窗口

    返回至少一个元素；text 为空返回 []。切分策略：
    1. 先按空行/换行拆成「段」，再把段按句子边界（。！？；.!?; 及换行）拆成「句单元」；
    2. 贪心地把句单元拼进当前窗口，超过 size 就开新窗口；
    3. 单个句单元本身就超过 size 时，按 size 硬切（带 overlap）。
    """
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0:
        return [text]
    if overlap < 0 or overlap >= size:
        overlap = max(0, min(overlap, size - 1))

    units = _to_sentence_units(text)

    windows: list[str] = []
    cur = ""
    for u in units:
        # 超长句单元：先冲掉当前窗口，再对它单独硬切
        if len(u) > size:
            if cur:
                windows.append(cur)
                cur = ""
            windows.extend(_hard_split(u, size=size, overlap=overlap))
            continue
        if not cur:
            cur = u
        elif len(cur) + 1 + len(u) <= size:
            cur = f"{cur} {u}"
        else:
            windows.append(cur)
            # 用上一窗口的尾部做 overlap 前缀，保证跨边界的语义连续
            tail = cur[-overlap:] if overlap else ""
            cur = f"{tail} {u}".strip() if tail else u
    if cur:
        windows.append(cur)

    # 去掉纯空白 / 重复窗口，保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for w in windows:
        w = w.strip()
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out or [text]


# 句子/子句边界：中英文句末标点 + 分号 + 换行。保留分隔符归属前一句。
_SENT_SEP = re.compile(r"(?<=[。！？；!?;])|\n+")


def _to_sentence_units(text: str) -> list[str]:
    """按段落 + 句子边界拆成句单元列表（已去空白、丢空串）。"""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        for s in _SENT_SEP.split(para):
            s = (s or "").strip()
            if s:
                units.append(s)
    return units


def _hard_split(s: str, *, size: int, overlap: int) -> list[str]:
    """对没有句子边界的超长串按字符硬切，步长 = size - overlap。"""
    step = max(1, size - overlap)
    return [s[i : i + size] for i in range(0, len(s), step)]
