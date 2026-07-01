"""Shared helpers for writing assistant Message bubbles to the chat history.

集中两个职责：
1. 把一段系统气泡内容落库（meta.kind 标记来源），返回 SSE/响应可直接 append 的 dict
2. 把"用户在知识预览面板勾选的条目"格式化成一条 Markdown 气泡 —— 让用户的每次确认
   都在聊天流里留痕，刷新可见。

不放在 routes_upload.py 里是因为 routes_generate.py 也要用，跨 route module 互相 import
比共用一个 _helpers 模块更怪。
"""
import json
from typing import Iterable

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.knowledge import KnowledgeEntry
from app.models.session import Message


KNOWLEDGE_TYPE_LABEL = {
    "product_rule": "产品规则",
    "module_relation": "模块关系",
    "defect_pattern": "缺陷模式",
    "term": "术语",
    "constraint": "约束",
}


def msg_payload(msg: Message) -> dict:
    """SSE/JSON 响应里给前端的 Message dict（meta 反序列化为 dict）。"""
    meta = None
    if msg.meta:
        try:
            meta = json.loads(msg.meta)
        except Exception:
            meta = None
    return {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
        "meta": meta,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


async def write_assistant_message(
    session_id: int, content: str, kind: str, ref: dict | None = None,
) -> dict:
    meta_obj: dict = {"kind": kind}
    if ref:
        meta_obj["ref"] = ref
    async with AsyncSessionLocal() as db:
        msg = Message(
            session_id=session_id,
            role="assistant",
            content=content,
            meta=json.dumps(meta_obj, ensure_ascii=False),
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        return msg_payload(msg)


def format_knowledge_selection_content(rows: Iterable[KnowledgeEntry], phase: str) -> str:
    """phase ∈ {'clarify','generate'}。把用户勾选的 KnowledgeEntry 渲染成 Markdown 气泡。

    空列表 → 一句话说明用户主动选择不注入。
    非空 → 列表，每条带类型标签 + 置信度 + 内容引用块（>240 字截断）。
    """
    rows = list(rows)
    target = "澄清提示词" if phase == "clarify" else "生成提示词"
    if not rows:
        return f"已选择不向{target}注入知识库条目，将仅基于本文档继续。"
    lines = [f"已确认向{target}注入 **{len(rows)}** 条知识库条目："]
    for e in rows:
        label = KNOWLEDGE_TYPE_LABEL.get(e.knowledge_type, e.knowledge_type)
        content = (e.content or "").strip()
        if len(content) > 240:
            content = content[:240] + "…"
        # 多行内容每行加 `> `，让 Markdown 渲染成一个完整的 quote 块挂在 list item 下面
        quoted = "\n  > ".join(content.split("\n"))
        lines.append(
            f"- **{label}** · 置信 {(e.confidence or 0) * 100:.0f}%\n  > {quoted}"
        )
    return "\n".join(lines)


async def record_knowledge_selection(
    *, session_id: int, project_id: int, phase: str,
    knowledge_ids: list[int] | None,
) -> dict | None:
    """把"用户在 KnowledgePreviewPanel 勾选的结果"作为系统气泡落库。

    knowledge_ids 语义：
      None → 不写气泡（自动 top-K 路径，用户没做选择）
      []   → 写一条"未注入"气泡（用户显式取消了所有勾选）
      非空 → 写带条目列表的气泡

    返回 None 表示没写（None 入参），调用方就不必 append assistant_message。
    """
    if knowledge_ids is None:
        return None
    rows: list[KnowledgeEntry] = []
    if knowledge_ids:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.id.in_(knowledge_ids),
                    KnowledgeEntry.project_id == project_id,
                )
            )
            rows = list(r.scalars().all())
    content = format_knowledge_selection_content(rows, phase)
    kind = "knowledge_selected_clarify" if phase == "clarify" else "knowledge_selected_generate"
    return await write_assistant_message(
        session_id, content, kind=kind,
        ref={"count": len(rows), "ids": [e.id for e in rows]},
    )
