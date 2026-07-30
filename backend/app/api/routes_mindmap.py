"""Test-mindmap generation API：把 PRD 提炼成测试脑图并写入飞书云文档。

独立于 routes_generate（生成用例）的平行功能：
  POST /api/mindmap/generate
    输入一份 PRD Document → LLM 产出 Markdown 大纲脑图 → lark-cli 建飞书文档
    → 把脑图作为一条 role='mindmap' 的 Document 落库（可被后续用例生成复用）
    → 返回飞书文档链接。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_project
from app.database import get_db
from app.models.session import Session, Message
from app.models.knowledge import Document, Module, KnowledgeEntry
from app.models.user import User
from app.agents.mindmap_generator import generate_mindmap
from app.prompts.registry import get_active_prompt_text
from app.knowledge.store import search_relevant, summarize_for_prompt
from app.config import get_settings
from app.tools.doc_parser import truncate_for_llm
from app.tools.lark_writer import (
    create_lark_doc,
    LarkCliNotInstalled,
    LarkCliNotLoggedIn,
    LarkPermissionDenied,
    LarkFetchTimeout,
    LarkFetchError,
)

logger = logging.getLogger("caseweave.routes_mindmap")

router = APIRouter()


class MindmapGenerateRequest(BaseModel):
    document_id: int  # PRD 文档 id（必填）
    # session_id 可选：从「知识库/模块详情」触发时没有会话上下文；给了才往聊天流追加系统气泡。
    session_id: int | None = None
    module_id: int | None = None
    module_name: str | None = None
    # 澄清阶段用户对每个问题的最终回答（{问题文本: 答案}）；无澄清时为 None。
    clarifications: dict[str, str] | None = None


@router.post("/mindmap/generate")
async def generate_test_mindmap(
    request: MindmapGenerateRequest,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """从 PRD 生成测试脑图，写入飞书文档，返回链接。"""
    # 若带了 session_id，校验其属于本项目（不带则跳过，纯文档级操作）
    if request.session_id is not None:
        result = await db.execute(
            select(Session).where(
                Session.id == request.session_id, Session.project_id == project_id
            )
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Session not found")

    # 加载 PRD 文档（scoped to project）
    result = await db.execute(
        select(Document).where(
            Document.id == request.document_id, Document.project_id == project_id
        )
    )
    prd_doc = result.scalar_one_or_none()
    if not prd_doc:
        raise HTTPException(status_code=404, detail="Document not found")

    prd_raw_text = prd_doc.raw_text or ""
    if not prd_raw_text.strip():
        raise HTTPException(status_code=422, detail="该文档没有可用于生成脑图的正文内容。")

    # 解析模块名（用户显式输入 > 关联 Module.name），与 routes_generate 口径一致
    module_name = (request.module_name or "").strip() or None
    linked_mid = request.module_id or prd_doc.module_id
    if linked_mid:
        result = await db.execute(
            select(Module).where(Module.id == linked_mid, Module.project_id == project_id)
        )
        module = result.scalar_one_or_none()
        if module and not module_name:
            module_name = module.name
    if not module_name:
        module_name = "未知模块"

    doc_content = truncate_for_llm(prd_raw_text)

    # 知识库命中：按 PRD 正文语义检索该项目/模块的相关知识条目，排除本文档自身抽取的条目
    # （避免自反馈污染），摘要后注入生成 prompt。与 routes_generate 的注入口径一致；失败静默降级。
    relevant_knowledge: str | None = None
    try:
        hits = await search_relevant(
            db, project_id=project_id, module_id=linked_mid,
            query=truncate_for_llm(prd_raw_text, limit=2000),
            top_k=get_settings().knowledge_inject_top_k,
        )
        if hits:
            hit_ids = [h.id for h in hits]
            same_doc = set((await db.execute(
                select(KnowledgeEntry.id).where(
                    KnowledgeEntry.id.in_(hit_ids),
                    KnowledgeEntry.document_id == prd_doc.id,
                )
            )).scalars().all())
            hits = [h for h in hits if h.id not in same_doc]
        relevant_knowledge = summarize_for_prompt(hits) or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("mindmap knowledge lookup failed (ignored): %s", exc)

    # 1) LLM 生成 Markdown 大纲脑图
    mindmap_system_prompt = await get_active_prompt_text(db, project_id, "mindmap_generator")
    try:
        markdown = await generate_mindmap(
            doc_content=doc_content,
            module_name=module_name,
            relevant_knowledge=relevant_knowledge,
            clarification_answers=request.clarifications or None,
            system_prompt=mindmap_system_prompt,
        )
    except Exception as exc:
        logger.exception("Mindmap LLM generation failed | doc_id=%s", request.document_id)
        raise HTTPException(status_code=502, detail=f"测试脑图生成失败：{exc}")
    if not markdown or not markdown.strip():
        raise HTTPException(status_code=502, detail="模型未返回有效的脑图内容，请重试。")

    # 2) 写入飞书文档
    title = f"{module_name} 测试脑图"
    try:
        lark_doc = await create_lark_doc(title=title, markdown=markdown)
    except LarkCliNotInstalled as exc:
        logger.exception("lark-cli not installed")
        raise HTTPException(status_code=500, detail=f"lark-cli 未安装：{exc}")
    except (LarkCliNotLoggedIn, LarkPermissionDenied, LarkFetchTimeout) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except LarkFetchError as exc:  # 含 LarkFetchFailed 及其他子类
        logger.exception("Lark create doc failed | title=%s", title)
        raise HTTPException(status_code=502, detail=f"写入飞书失败：{exc}")

    # 3) 把脑图落成 role='mindmap' 的 Document —— 天然可被后续「生成用例」当脑图输入复用
    mindmap_doc = Document(
        project_id=project_id,
        filename=title,
        module_id=linked_mid,
        file_type="lark_doc",
        source_type="lark",
        source_url=lark_doc.url[:500],
        role="mindmap",
        raw_text=markdown,
    )
    db.add(mindmap_doc)
    await db.commit()
    await db.refresh(mindmap_doc)

    # 4) 若有会话上下文，追加 assistant 系统气泡（可点击链接）
    assistant_message = None
    if request.session_id is not None:
        msg_meta = {
            "kind": "mindmap_generated",
            "ref": {
                "url": lark_doc.url,
                "title": title,
                "document_id": mindmap_doc.id,
                "module": module_name,
            },
        }
        msg = Message(
            session_id=request.session_id,
            role="assistant",
            content=f"已根据文档《{prd_doc.filename}》生成模块「{module_name}」的测试脑图，并存入飞书文档：[{title}]({lark_doc.url})",
            meta=json.dumps(msg_meta, ensure_ascii=False),
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        assistant_message = {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "meta": msg_meta,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        }

    return {
        "url": lark_doc.url,
        "title": title,
        "document_id": mindmap_doc.id,
        "markdown": markdown,
        "assistant_message": assistant_message,
    }
