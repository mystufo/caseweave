"""Chat API: session management and streaming conversation."""
import json
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, desc, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from app.auth import get_current_user, require_project
from app.limits import Ticket, llm_gate, llm_ticket
from app.database import get_db
from app.models.session import Session, Message
from app.models.clarification import ClarificationState
from app.models.knowledge import Document
from app.models.feedback import TestCase, Feedback
from app.models.user import User
from app.config import get_settings
from app.agents.llm_factory import build_chat_model

router = APIRouter()
settings = get_settings()


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: int | None = None
    message: str
    module_id: int | None = None


class SessionCreate(BaseModel):
    title: str = "New Session"
    module_id: int | None = None
    mode: str = "cases"  # cases（生成用例，默认）/ mindmap（生成测试脑图）


class SessionUpdate(BaseModel):
    title: str


# ── Streaming helper ──────────────────────────────────────────────────────────

async def _stream_claude(
    messages: list[dict],
    session_id: int,
    db: AsyncSession,
) -> AsyncGenerator[str, None]:
    """Stream LLM response as SSE events and persist the full reply."""
    full_response = ""

    system = (
        "你是 CaseWeave（纬策），一个智能测试用例生成助手。"
        "你帮助测试工程师从产品需求文档中生成完整的测试用例。"
        "在分析文档时，请先识别歧义和缺失点并主动提问，确认后再生成测试用例。"
        "回答时使用中文，代码和JSON使用原始格式。"
    )

    lc_messages: list = [SystemMessage(content=system)]
    for m in messages:
        if m["role"] == "user":
            lc_messages.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            lc_messages.append(AIMessage(content=m["content"]))

    try:
        llm = build_chat_model(max_tokens=4096, temperature=0.2)
        async for chunk in llm.astream(lc_messages):
            text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
            if not text:
                continue
            full_response += text
            yield f"data: {json.dumps({'type': 'text', 'content': text}, ensure_ascii=False)}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        return

    db_msg = Message(session_id=session_id, role="assistant", content=full_response)
    db.add(db_msg)
    await db.commit()

    yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ── Routes ────────────────────────────────────────────────────────────────────

async def _load_session_scoped(session_id: int, project_id: int, db: AsyncSession) -> Session:
    result = await db.execute(
        select(Session).where(Session.id == session_id, Session.project_id == project_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/sessions")
async def create_session(
    data: SessionCreate,
    project_id: int = Depends(require_project),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = Session(
        title=data.title,
        module_id=data.module_id,
        mode=data.mode if data.mode in ("cases", "mindmap") else "cases",
        project_id=project_id,
        user_id=user.id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return {
        "id": session.id,
        "title": session.title,
        "mode": session.mode,
        "status": session.status,
        "created_at": session.created_at,
    }


@router.get("/sessions")
async def list_sessions(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Session)
        .where(Session.project_id == project_id)
        .order_by(desc(Session.created_at))
        .limit(50)
    )
    sessions = result.scalars().all()
    return [
        {
            "id": s.id,
            "title": s.title,
            "mode": s.mode,
            "status": s.status,
            "created_at": s.created_at,
        }
        for s in sessions
    ]


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: int,
    data: SessionUpdate,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    title = data.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    if len(title) > 255:
        title = title[:255]

    session = await _load_session_scoped(session_id, project_id, db)
    session.title = title
    await db.commit()
    await db.refresh(session)
    return {
        "id": session.id,
        "title": session.title,
        "mode": session.mode,
        "status": session.status,
        "created_at": session.created_at,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除会话及其从属数据。手动按序清子表——init_db 建表未走迁移，不能保证 DB
    层真的建了 ON DELETE CASCADE 外键，这里显式删更稳妥。Document 不随会话删除
    （按 sha256 复用、可能被其他会话共享），仅解绑无需处理——Document 不引用 session。"""
    session = await _load_session_scoped(session_id, project_id, db)

    # Feedback（挂在 test_case 上）→ TestCase → Message / ClarificationState → Session
    case_ids = (await db.execute(
        select(TestCase.id).where(TestCase.session_id == session_id)
    )).scalars().all()
    if case_ids:
        await db.execute(sa_delete(Feedback).where(Feedback.test_case_id.in_(case_ids)))
    await db.execute(sa_delete(TestCase).where(TestCase.session_id == session_id))
    await db.execute(sa_delete(Message).where(Message.session_id == session_id))
    await db.execute(sa_delete(ClarificationState).where(ClarificationState.session_id == session_id))
    await db.delete(session)
    await db.commit()
    return {"deleted": True, "id": session_id}


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _load_session_scoped(session_id, project_id, db)
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at)
    )
    messages = result.scalars().all()
    out = []
    for m in messages:
        meta = None
        if m.meta:
            try:
                meta = json.loads(m.meta)
            except Exception:
                meta = None
        out.append({
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "meta": meta,
            "created_at": m.created_at,
        })
    return out


@router.get("/sessions/{session_id}/clarification_state")
async def get_clarification_state(
    session_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回当前会话的澄清运行态——前端进入会话时一并拉取，复原 SessionState。
    会话不存在 → 404；状态尚未建立（用户还没上传任何文档）→ 也返回 404。"""
    await _load_session_scoped(session_id, project_id, db)
    r = await db.execute(
        select(ClarificationState).where(ClarificationState.session_id == session_id)
    )
    st = r.scalar_one_or_none()
    if st is None:
        raise HTTPException(status_code=404, detail="No clarification state yet")

    # JOIN 出 PRD / 脑图 文档摘要，让前端会话回放一次到位（不用再单独 GET documents）
    prd_filename: str | None = None
    prd_stats: dict | None = None
    mindmap_filename: str | None = None
    mindmap_stats: dict | None = None
    prd_pending_drafts: list | None = None
    mindmap_pending_drafts: list | None = None
    module_id_by_doc: dict[int, int | None] = {}
    doc_ids = [x for x in (st.document_id, st.mindmap_document_id) if x is not None]
    if doc_ids:
        rd = await db.execute(
            select(Document).where(
                Document.id.in_(doc_ids), Document.project_id == project_id
            )
        )
        for d in rd.scalars().all():
            module_id_by_doc[d.id] = d.module_id
            stats = {
                "chunks": len(d.parsed_content or []),
                "tables": 0,
                "raw_text_length": len(d.raw_text or ""),
            }
            if d.id == st.document_id:
                prd_filename = d.filename
                prd_stats = stats
                prd_pending_drafts = d.pending_knowledge if isinstance(d.pending_knowledge, list) else None
            if d.id == st.mindmap_document_id:
                mindmap_filename = d.filename
                mindmap_stats = stats
                mindmap_pending_drafts = d.pending_knowledge if isinstance(d.pending_knowledge, list) else None

    # 重建「建议新建模块」卡片——它原本只由上传时的 SSE 帧填充，刷新后会丢。
    # 数据源是已落库的 module_auto_classified 系统气泡（meta.ref.proposed_module）。
    # 只在满足以下条件时复原，避免把已处理的提议又弹回来：
    #   - applied=false（当时不是高置信自动归类）
    #   - 引用文档仍未归类（module_id IS NULL）——用户点过「创建并归类」后 module_id 会被写上，提议即作废。
    # 取时间上最近的一条。用户点「忽略」是纯前端操作，刷新后该提议会再次出现（属可接受行为：这只是一个建议）。
    module_proposal: dict | None = None
    mrows = await db.execute(
        select(Message)
        .where(Message.session_id == session_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
    )
    for m in mrows.scalars().all():
        if not m.meta:
            continue
        try:
            meta = json.loads(m.meta)
        except Exception:
            continue
        if meta.get("kind") != "module_auto_classified":
            continue
        ref = meta.get("ref") or {}
        prop = ref.get("proposed_module")
        if not prop or ref.get("applied"):
            continue
        doc_id = ref.get("document_id")
        # 引用文档已归类（用户已确认或高置信自动归类）→ 提议作废，跳过
        if doc_id is not None and module_id_by_doc.get(doc_id) is not None:
            continue
        module_proposal = {
            "document_id": doc_id,
            "name": prop.get("name") or "",
            "code": prop.get("code") or "",
            "description": prop.get("description"),
        }
        break

    return {
        "session_id": st.session_id,
        "document_id": st.document_id,
        "mindmap_document_id": st.mindmap_document_id,
        "prd_filename": prd_filename,
        "prd_stats": prd_stats,
        "mindmap_filename": mindmap_filename,
        "mindmap_stats": mindmap_stats,
        "prd_pending_drafts": prd_pending_drafts,
        "mindmap_pending_drafts": mindmap_pending_drafts,
        "module_proposal": module_proposal,
        "summary": st.summary,
        "module_detected": st.module_detected,
        "case_prefix_suggestion": st.case_prefix_suggestion,
        "confirmed_module_name": st.confirmed_module_name,
        "confirmed_case_prefix": st.confirmed_case_prefix,
        "current_round": st.current_round,
        "rounds": st.rounds or [],
        "current_questions": st.current_questions or [],
        "ready_to_generate": st.ready_to_generate,
        "status": st.status,
        "updated_at": st.updated_at.isoformat() if st.updated_at else None,
    }


DEFAULT_TITLES = {"新会话", "New Session"}


def _summarize(text: str, limit: int = 30) -> str:
    snippet = text.strip().splitlines()[0] if text.strip() else ""
    return snippet[:limit] + ("…" if len(snippet) > limit else "")


@router.post("/chat")
async def chat(
    request: ChatRequest,
    project_id: int = Depends(require_project),
    user: User = Depends(get_current_user),
    _ticket: Ticket = Depends(llm_ticket),  # 并发闸门 + 每日配额
    db: AsyncSession = Depends(get_db),
):
    # Resolve or create session (scoped to project)
    if request.session_id:
        session = await _load_session_scoped(request.session_id, project_id, db)
        if session.title in DEFAULT_TITLES:
            session.title = _summarize(request.message)
    else:
        session = Session(
            title=_summarize(request.message),
            module_id=request.module_id,
            project_id=project_id,
            user_id=user.id,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

    user_msg = Message(session_id=session.id, role="user", content=request.message)
    db.add(user_msg)
    await db.commit()

    result = await db.execute(
        select(Message)
        .where(Message.session_id == session.id)
        .order_by(Message.created_at)
        .limit(40)
    )
    history = result.scalars().all()
    lc_messages = [
        {"role": m.role, "content": m.content}
        for m in history
        if m.role in ("user", "assistant")
    ]

    async def event_stream():
        yield f"data: {json.dumps({'type': 'session', 'session_id': session.id})}\n\n"
        async for chunk in _stream_claude(lc_messages, session.id, db):
            yield chunk

    return StreamingResponse(
        llm_gate.wrap_stream(_ticket, event_stream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
