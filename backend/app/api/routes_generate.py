"""Test case generation and export API."""
import asyncio
import json
import re
from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import authorize_project, decode_token, get_current_user, require_project
from app.database import get_db
from app.models.session import Session, Message
from app.models.knowledge import Document, Module, ModuleRelation, KnowledgeEntry, Skill
from app.models.feedback import TestCase
from app.models.clarification import ClarificationState
from app.models.user import User
from app.agents.generator import generate_test_cases, stream_generate_test_cases
from app.knowledge.store import search_relevant, summarize_for_prompt, HitEntry
from app.api._assistant_messages import record_knowledge_selection
from app.config import get_settings
from app.prompts.registry import get_active_prompt_text
from app.tools.doc_parser import truncate_for_llm
from app.tools.excel_export import export_test_cases

router = APIRouter()

CASE_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,39}$")
VALID_PRIORITIES = {"P1", "P2", "P3"}


def _normalize_priority(raw: str | None) -> str:
    """Coerce LLM output into P1/P2/P3; fall back to P2 when invalid/missing."""
    if not raw:
        return "P2"
    p = str(raw).strip().upper()
    if p in VALID_PRIORITIES:
        return p
    # Tolerate variants like "p1", "高/中/低", "1/2/3"
    if p in {"高", "HIGH", "1"}:
        return "P1"
    if p in {"中", "MEDIUM", "MID", "2"}:
        return "P2"
    if p in {"低", "LOW", "3"}:
        return "P3"
    return "P2"


class GenerateRequest(BaseModel):
    session_id: int
    # PRD 文档 id：与 mindmap_document_id 至少需要一个非空（路由内交叉校验）
    document_id: int | None = None
    mindmap_document_id: int | None = None
    module_id: int | None = None
    module_name: str | None = None  # User-confirmed module name; overrides per-case module
    case_prefix: str | None = None  # e.g. USER_LOGIN — enforced as TC-{prefix}-... on all rows
    clarification_answers: dict[str, str] | None = None
    # Phase 3: 用户在前端预览面板选好的知识条目 id 列表。
    #   None  → 旧行为：自动 search_relevant top-K 注入（保留向后兼容）
    #   []    → 显式不注入任何知识（用户全取消勾选）
    #   非空  → 仅按这些 id 加载并注入，跳过自动 search
    knowledge_ids: list[int] | None = None
    stream: bool = False


def _normalize_case_number(raw: str, prefix: str, fallback_index: int) -> str:
    """
    Ensure case_number begins with `{prefix}-`.
    Strip any stray leading `TC-` the model may have added, then splice our prefix in.
    Note: prefix itself may contain dashes (e.g. USER-LOGIN), so we match the full head literally.
    """
    expected_head = f"{prefix}-"
    raw = (raw or "").strip()
    # Strip stray TC- the model sometimes prepends
    if raw.upper().startswith("TC-"):
        raw = raw[3:]
    if raw.startswith(expected_head):
        return raw
    # Model used a different prefix entirely — keep just the trailing index-ish part if we can find one.
    # Heuristic: take the last dash-separated segment(s) that look like SUB-NNN or NNN.
    parts = raw.split("-")
    tail_parts: list[str] = []
    for token in reversed(parts):
        if not token:
            continue
        tail_parts.insert(0, token)
        if token.isdigit():
            break
    suffix = "-".join(tail_parts) if tail_parts else f"{fallback_index:03d}"
    if not suffix:
        suffix = f"{fallback_index:03d}"
    return f"{prefix}-{suffix}"


@router.post("/generate")
async def generate(
    request: GenerateRequest,
    raw_request: Request,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate test cases from an uploaded document."""
    # Validate case prefix (required for consistent numbering)
    case_prefix = (request.case_prefix or "").strip().upper()
    if not case_prefix:
        raise HTTPException(status_code=400, detail="case_prefix is required")
    if not CASE_PREFIX_RE.match(case_prefix):
        raise HTTPException(
            status_code=400,
            detail="case_prefix must be 1–40 chars, uppercase A–Z / digits / dashes, starting with a letter (e.g. USER-LOGIN).",
        )

    # Validate session belongs to this project
    result = await db.execute(
        select(Session).where(Session.id == request.session_id, Session.project_id == project_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Session not found")

    if request.document_id is None and request.mindmap_document_id is None:
        raise HTTPException(status_code=400, detail="至少需要提供 PRD 文档或测试脑图其中之一。")

    # Load documents (PRD and/or mindmap), each scoped to project
    prd_doc: Document | None = None
    mindmap_doc: Document | None = None
    if request.document_id is not None:
        result = await db.execute(
            select(Document).where(
                Document.id == request.document_id, Document.project_id == project_id
            )
        )
        prd_doc = result.scalar_one_or_none()
        if not prd_doc:
            raise HTTPException(status_code=404, detail="Document not found")
    if request.mindmap_document_id is not None:
        result = await db.execute(
            select(Document).where(
                Document.id == request.mindmap_document_id, Document.project_id == project_id
            )
        )
        mindmap_doc = result.scalar_one_or_none()
        if not mindmap_doc:
            raise HTTPException(status_code=404, detail="Mindmap document not found")

    # Resolve module: explicit user input wins for the name; we still load the linked
    # Module row to pick up its `code` (English name = case_prefix), so that generated
    # cases' module name + number prefix stay aligned with the module.
    module_name = (request.module_name or "").strip() or None
    linked_mid = (
        request.module_id
        or (prd_doc.module_id if prd_doc else None)
        or (mindmap_doc.module_id if mindmap_doc else None)
    )
    if linked_mid:
        result = await db.execute(
            select(Module).where(Module.id == linked_mid, Module.project_id == project_id)
        )
        module = result.scalar_one_or_none()
        if module:
            if not module_name:
                module_name = module.name
            # 模块有英文名(code) → 用它作为用例编号前缀，覆盖前端传入/澄清建议的 prefix，
            # 保证「模块 / 用例 module 字段 / case_number 前缀」三者一致。
            if module.code:
                case_prefix = module.code
    if not module_name:
        module_name = "未知模块"

    prd_raw_text = (prd_doc.raw_text or "") if prd_doc else ""
    mindmap_raw_text = (mindmap_doc.raw_text or "") if mindmap_doc else ""
    doc_content = truncate_for_llm(prd_raw_text) if prd_raw_text else ""
    mindmap_content = truncate_for_llm(mindmap_raw_text) if mindmap_raw_text else None

    # Phase 3：构造 Generator 用的产品知识上下文。
    # - 用户在前端预览面板选了具体条目（knowledge_ids 非 None）→ 只用这些
    # - 没传（None）→ 老路径自动 top-K（保留兼容；老前端 / 命令行调试不会断）
    # 知识检索是辅助，整段 try/except 静默降级到 None，绝不阻塞用例生成。
    relevant_knowledge: str | None = None
    # 自动检索时的 query：脑图代表测试人员最终意图，更接近"我们要测什么"，优先用脑图原文
    knowledge_query_src = mindmap_raw_text or prd_raw_text
    knowledge_module_id = (
        (prd_doc.module_id if prd_doc else None)
        or (mindmap_doc.module_id if mindmap_doc else None)
    )
    knowledge_self_doc_id = prd_doc.id if prd_doc else (mindmap_doc.id if mindmap_doc else None)
    try:
        if request.knowledge_ids is None:
            hits = await search_relevant(
                db, project_id=project_id, module_id=knowledge_module_id,
                query=truncate_for_llm(knowledge_query_src, limit=2000),
                top_k=get_settings().knowledge_inject_top_k,
            )
            if hits and knowledge_self_doc_id is not None:
                # 同文档抽取出的条目排除掉，避免自反馈污染
                hit_ids = [h.id for h in hits]
                rows = (await db.execute(
                    select(KnowledgeEntry.id).where(
                        KnowledgeEntry.id.in_(hit_ids),
                        KnowledgeEntry.document_id == knowledge_self_doc_id,
                    )
                )).scalars().all()
                same_doc = set(rows)
                hits = [h for h in hits if h.id not in same_doc]
            relevant_knowledge = summarize_for_prompt(hits) or None
        elif request.knowledge_ids:
            # 项目内 + 仅按 id 取，避免越权拉到别的项目的条目
            rows = (await db.execute(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.id.in_(request.knowledge_ids),
                    KnowledgeEntry.project_id == project_id,
                )
            )).scalars().all()
            # 套到 HitEntry shape 让 summarize_for_prompt 一视同仁；distance 不参与排序，置 None
            faux_hits = [
                HitEntry(
                    id=e.id, knowledge_type=e.knowledge_type, content=e.content,
                    confidence=float(e.confidence or 0.0), distance=None,
                )
                for e in rows
            ]
            relevant_knowledge = summarize_for_prompt(faux_hits) or None
        # knowledge_ids == [] → relevant_knowledge 保持 None，显式跳过注入
    except Exception:
        relevant_knowledge = None

    # ── Phase 3：自动注入模块关联关系 ──────────────────────────────────────────
    # 从 module_relations 表里取与本次生成所属模块（knowledge_module_id 或 module_name 解析出来的 id）
    # 相关的所有关系，拼成 prompt 片段。让 LLM 在生成跨模块联动用例时考虑上下游。
    # 失败静默退化为 None（不阻塞生成主流程）。
    module_relations_str: str | None = None
    relation_module_id = (
        request.module_id
        or knowledge_module_id
        or (None if module_name == "未知模块" else None)
    )
    if relation_module_id is None and module_name and module_name != "未知模块":
        # 用户传的是 module_name，没传 id：按 (project, name) 反查一次
        m_q = await db.execute(
            select(Module.id).where(Module.project_id == project_id, Module.name == module_name)
        )
        relation_module_id = m_q.scalar_one_or_none()
    try:
        if relation_module_id is not None:
            rel_rows = (await db.execute(
                select(ModuleRelation).where(
                    (ModuleRelation.source_module_id == relation_module_id)
                    | (ModuleRelation.target_module_id == relation_module_id)
                )
            )).scalars().all()
            if rel_rows:
                # 拉模块名做可读化
                ids = {r.source_module_id for r in rel_rows} | {r.target_module_id for r in rel_rows}
                mods = (await db.execute(
                    select(Module).where(Module.id.in_(ids), Module.project_id == project_id)
                )).scalars().all()
                name_map = {m.id: m.name for m in mods}
                lines: list[str] = []
                for r in rel_rows:
                    src = name_map.get(r.source_module_id, f"#{r.source_module_id}")
                    tgt = name_map.get(r.target_module_id, f"#{r.target_module_id}")
                    desc = f"（{r.description}）" if r.description else ""
                    lines.append(f"- {src} —[{r.relation_type}]→ {tgt}{desc}")
                module_relations_str = "\n".join(lines)
    except Exception:
        module_relations_str = None

    # ── Phase 4：注入该模块下的 Skills（最近 3 条 Markdown 备忘单） ─────────────
    # Skills 是"已经被人验证过"的测试设计经验，比 KnowledgeEntry 更高优先级。
    # 取最近更新的若干条，按 \n\n--- 拼接，整段塞进 generator 的 skills 参数。
    skills_str: str | None = None
    if relation_module_id is not None:
        try:
            sk_rows = (await db.execute(
                select(Skill)
                .where(Skill.module_id == relation_module_id)
                .order_by(Skill.updated_at.desc().nullslast(), Skill.id.desc())
                .limit(3)
            )).scalars().all()
            if sk_rows:
                pieces = [s.content.strip() for s in sk_rows if (s.content or "").strip()]
                if pieces:
                    skills_str = "\n\n---\n\n".join(pieces)
        except Exception:
            skills_str = None

    # 在请求 session 仍有效时解析激活的生成提示词；流式分支的 event_stream()
    # 运行时外层 db 可能已随响应关闭，故提前取好再传入。
    generator_system_prompt = await get_active_prompt_text(db, project_id, "generator")

    if request.stream:
        async def event_stream():
            buffer = ""
            async for token in stream_generate_test_cases(
                doc_content=doc_content,
                module_name=module_name,
                case_prefix=case_prefix,
                clarification_answers=request.clarification_answers,
                skills=skills_str,
                module_relations=module_relations_str,
                relevant_knowledge=relevant_knowledge,
                mindmap_content=mindmap_content,
                system_prompt=generator_system_prompt,
            ):
                buffer += token
                yield f"data: {json.dumps({'type': 'text', 'content': token}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 用户在 KnowledgePreviewPanel 上的"已确认 N 条"作为一条系统气泡先落库——
    # 即使 LLM 调用挂了，用户至少能在聊天流里看到自己刚才做了什么选择。
    selection_msg = await record_knowledge_selection(
        session_id=request.session_id, project_id=project_id, phase="generate",
        knowledge_ids=request.knowledge_ids,
    )

    # Non-streaming: generate and persist.
    # 前端"停止任务"会 abort axios 请求 → 关闭连接。非流式 handler 默认不会因此被取消
    # （它从不 await receive()），LLM 调用会白跑到底。这里让 LLM 任务与"断连轮询"竞速：
    # 客户端一断开就 cancel 掉 llm_task → 取消底层 httpx 请求 → 真正停止到大模型的调用。
    llm_task = asyncio.create_task(generate_test_cases(
        doc_content=doc_content,
        module_name=module_name,
        case_prefix=case_prefix,
        clarification_answers=request.clarification_answers,
        skills=skills_str,
        module_relations=module_relations_str,
        relevant_knowledge=relevant_knowledge,
        mindmap_content=mindmap_content,
        system_prompt=generator_system_prompt,
    ))

    async def _watch_disconnect():
        # 轮询客户端连接状态；断开即返回，触发上面 llm_task 的取消。
        while True:
            if await raw_request.is_disconnected():
                return
            await asyncio.sleep(0.5)

    watch_task = asyncio.create_task(_watch_disconnect())
    done, _pending = await asyncio.wait(
        {llm_task, watch_task}, return_when=asyncio.FIRST_COMPLETED
    )

    if llm_task in done:
        # LLM 正常跑完：停掉轮询，取回结果继续落库。
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        cases = llm_task.result()
    else:
        # 客户端已断开：取消 LLM 任务（连带取消到大模型的 httpx 请求），不落库直接返回。
        llm_task.cancel()
        try:
            await llm_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        # 客户端已断开，返回体不会被读取；499 = Client Closed Request。
        return Response(status_code=499)

    # Persist test cases. Force every row's module + case_number prefix so the
    # whole batch stays consistent regardless of what the LLM emitted.
    db_cases = []
    for idx, case in enumerate(cases, start=1):
        normalized_no = _normalize_case_number(case.get("case_number", ""), case_prefix, idx)
        db_case = TestCase(
            project_id=project_id,
            session_id=request.session_id,
            case_number=normalized_no,
            name=case.get("name", ""),
            module=module_name,
            priority=_normalize_priority(case.get("priority")),
            preconditions=case.get("preconditions", ""),
            steps=case.get("steps", ""),
            expected_result=case.get("expected_result", ""),
            remarks=case.get("remarks", ""),
        )
        db.add(db_case)
        db_cases.append(db_case)

    await db.commit()
    for c in db_cases:
        await db.refresh(c)

    # 系统气泡 + 终结澄清运行态
    gen_msg_meta = {"kind": "generate_done", "ref": {
        "total": len(db_cases),
        "module": module_name,
        "case_prefix": case_prefix,
    }}
    gen_msg = Message(
        session_id=request.session_id,
        role="assistant",
        content=f"已按模块「{module_name}」（编号前缀 {case_prefix}）生成 **{len(db_cases)}** 条测试用例。",
        meta=json.dumps(gen_msg_meta, ensure_ascii=False),
    )
    db.add(gen_msg)

    state_q = await db.execute(
        select(ClarificationState).where(ClarificationState.session_id == request.session_id)
    )
    state = state_q.scalar_one_or_none()
    if state is not None:
        state.status = "done"
        state.confirmed_module_name = module_name
        state.confirmed_case_prefix = case_prefix
        state.ready_to_generate = True
        state.current_questions = []

    await db.commit()
    await db.refresh(gen_msg)

    return {
        "session_id": request.session_id,
        "total": len(cases),
        "knowledge_selection_message": selection_msg,
        "assistant_message": {
            "id": gen_msg.id,
            "role": gen_msg.role,
            "content": gen_msg.content,
            "meta": gen_msg_meta,
            "created_at": gen_msg.created_at.isoformat() if gen_msg.created_at else None,
        },
        "cases": [
            {
                "id": c.id,
                "case_number": c.case_number,
                "name": c.name,
                "module": c.module,
                "priority": c.priority or "P2",
                "preconditions": c.preconditions,
                "steps": c.steps,
                "expected_result": c.expected_result,
                "remarks": c.remarks,
            }
            for c in db_cases
        ],
    }


@router.get("/cases")
async def list_all_cases(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return every test case in this project, newest first, with session title."""
    result = await db.execute(
        select(TestCase, Session.title)
        .join(Session, Session.id == TestCase.session_id)
        .where(TestCase.project_id == project_id)
        .order_by(TestCase.created_at.desc(), TestCase.id.desc())
    )
    rows = result.all()
    return {
        "total": len(rows),
        "cases": [
            {
                "id": c.id,
                "session_id": c.session_id,
                "session_title": title,
                "case_number": c.case_number,
                "name": c.name,
                "module": c.module or "未分组",
                "priority": c.priority or "P2",
                "preconditions": c.preconditions,
                "steps": c.steps,
                "expected_result": c.expected_result,
                "remarks": c.remarks,
                "test_result": c.test_result or "",
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c, title in rows
        ],
    }


@router.get("/sessions/{session_id}/cases")
async def list_session_cases(
    session_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return all test cases previously generated for a session."""
    result = await db.execute(
        select(TestCase)
        .where(TestCase.session_id == session_id, TestCase.project_id == project_id)
        .order_by(TestCase.id)
    )
    cases = result.scalars().all()
    return {
        "session_id": session_id,
        "total": len(cases),
        "cases": [
            {
                "id": c.id,
                "case_number": c.case_number,
                "name": c.name,
                "module": c.module,
                "priority": c.priority or "P2",
                "preconditions": c.preconditions,
                "steps": c.steps,
                "expected_result": c.expected_result,
                "remarks": c.remarks,
            }
            for c in cases
        ],
    }


@router.delete("/cases/{case_id}")
async def delete_case(
    case_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a test case (and its cascading feedback rows) within this project."""
    result = await db.execute(
        select(TestCase).where(TestCase.id == case_id, TestCase.project_id == project_id)
    )
    case = result.scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=404, detail="Test case not found")
    await db.delete(case)
    await db.commit()
    return {"id": case_id, "deleted": True}


@router.post("/export/cases")
async def export_filtered_cases(
    body: dict,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export an arbitrary set of test cases (by id) as Excel.

    Body: `{"case_ids": [1, 2, 3, ...]}`. Cases are scoped to the current
    project; ids not in this project are silently dropped.
    """
    ids = body.get("case_ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="case_ids 不能为空")
    try:
        ids_int = [int(x) for x in ids]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="case_ids 必须是整数列表")

    result = await db.execute(
        select(TestCase)
        .where(TestCase.id.in_(ids_int), TestCase.project_id == project_id)
        .order_by(TestCase.module.asc(), TestCase.case_number.asc())
    )
    cases = result.scalars().all()
    if not cases:
        raise HTTPException(status_code=404, detail="没有可导出的用例")

    case_dicts = [
        {
            "case_number": c.case_number,
            "name": c.name,
            "module": c.module,
            "priority": c.priority or "P2",
            "preconditions": c.preconditions,
            "steps": c.steps,
            "expected_result": c.expected_result,
            "remarks": c.remarks,
            "test_result": c.test_result or "",
        }
        for c in cases
    ]

    xlsx_bytes = export_test_cases(case_dicts, group_by_module=True)
    filename = f"testcases_filtered_{len(cases)}.xlsx"

    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/{session_id}")
async def export_session(
    session_id: int,
    token: str | None = Query(default=None),
    project_id_q: int | None = Query(default=None, alias="project_id"),
    authorization: str | None = Header(default=None),
    x_project_id: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Export all test cases for a session as Excel.

    Browser anchor tags can't set headers, so this endpoint also accepts
    ?token=...&project_id=... in the query string.
    """
    # Resolve auth — header first, fall back to query token.
    tok: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization.split(" ", 1)[1].strip()
    elif token:
        tok = token.strip()
    if not tok:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        payload = decode_token(tok)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Resolve project — header first, fall back to query.
    project_id: int | None = None
    if x_project_id:
        try:
            project_id = int(x_project_id)
        except ValueError:
            pass
    if project_id is None:
        project_id = project_id_q
    if not project_id:
        raise HTTPException(status_code=400, detail="Missing project_id")

    # Verify user exists
    user_id = int(payload.get("sub", 0))
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # 项目访问权限：管理员或项目创建者
    await authorize_project(project_id, user, db)

    result = await db.execute(
        select(Session).where(Session.id == session_id, Session.project_id == project_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    result = await db.execute(
        select(TestCase).where(TestCase.session_id == session_id, TestCase.project_id == project_id)
    )
    cases = result.scalars().all()
    if not cases:
        raise HTTPException(status_code=404, detail="No test cases found for this session")

    case_dicts = [
        {
            "case_number": c.case_number,
            "name": c.name,
            "module": c.module,
            "priority": c.priority or "P2",
            "preconditions": c.preconditions,
            "steps": c.steps,
            "expected_result": c.expected_result,
            "remarks": c.remarks,
            "test_result": c.test_result or "",
        }
        for c in cases
    ]

    xlsx_bytes = export_test_cases(case_dicts, group_by_module=True)
    filename = f"testcases_session_{session_id}.xlsx"

    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
