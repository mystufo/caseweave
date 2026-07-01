"""Upload & document parsing API."""
import asyncio
import hashlib
import json
import logging
import time
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.auth import get_current_user, require_project
from app.database import AsyncSessionLocal, get_db
from app.models.knowledge import Document, Module, KnowledgeEntry
from app.models.session import Session, Message
from app.models.clarification import ClarificationState
from app.models.user import User
from app.tools.doc_parser import parse_document, truncate_for_llm
from app.tools.mindmap_parser import parse_mindmap_md
from app.tools.lark_fetcher import (
    classify_lark_url,
    fetch_lark_content,
    LarkFetchError,
    LarkUrlInvalid,
    LarkSheetUnsupported,
    LarkCliNotInstalled,
    LarkCliNotLoggedIn,
    LarkPermissionDenied,
    LarkFetchTimeout,
    LarkEmptyDoc,
)
from app.agents.clarifier import (
    analyze_document_for_clarification,
    stream_analyze_document_for_clarification,
    stream_followup_clarification,
)
from app.agents.knowledge_extractor import extract_knowledge
from app.agents.module_classifier import classify_module, ModuleSuggestion
from app.knowledge.store import (
    store_entries, search_relevant, summarize_for_prompt, HitEntry,
    find_similar_entries,
)
from app.api._assistant_messages import record_knowledge_selection
from app.config import get_settings
from app.prompts.registry import get_active_prompt_text, resolve_active_prompt


async def _fetch_knowledge_brief(
    *, project_id: int, module_id: int | None, query: str, top_k: int | None = None,
    document_id: int | None = None,
) -> str | None:
    """Best-effort: pull top-K relevant knowledge entries for a query and summarize.

    Returns None when nothing is available — callers can pass that straight through
    to Clarifier/Generator (None means "no extra context").

    document_id 给出时排除"由这份文档自身抽取出"的条目，避免 Clarifier/Generator
    被自反馈污染（同文档刚被异步抽取写入的 entry 不该再当外部上下文喂回去）。

    top_k None → 用 settings.knowledge_inject_top_k
    """
    if not (query or "").strip():
        return None
    if top_k is None:
        top_k = get_settings().knowledge_inject_top_k
    try:
        async with AsyncSessionLocal() as db:
            hits = await search_relevant(
                db, project_id=project_id, module_id=module_id,
                query=query, top_k=top_k,
            )
            if document_id is not None and hits:
                hit_ids = [h.id for h in hits]
                rows = (await db.execute(
                    select(KnowledgeEntry.id).where(
                        KnowledgeEntry.id.in_(hit_ids),
                        KnowledgeEntry.document_id == document_id,
                    )
                )).scalars().all()
                same_doc = set(rows)
                hits = [h for h in hits if h.id not in same_doc]
        return summarize_for_prompt(hits) or None
    except Exception as exc:
        logger.warning("knowledge lookup failed: %s", exc)
        return None


def _knowledge_hit_dict(e: KnowledgeEntry, distance: float | None) -> dict:
    """SSE knowledge_preview 帧用的精简 dict —— 与 GET /api/knowledge/preview 同 shape，
    让前端复用现有的 KnowledgePreviewPanel 组件不用判分支。"""
    return {
        "id": e.id,
        "module_id": e.module_id,
        "document_id": e.document_id,
        "knowledge_type": e.knowledge_type,
        "content": e.content,
        "source": e.source,
        "confidence": float(e.confidence or 0.0),
        "version": e.version,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "distance": distance,
    }


def _format_auto_classify_message(
    *, module_name: str | None, suggestion: ModuleSuggestion, applied: bool,
) -> str:
    """生成一条对用户可读的"自动归类"系统气泡文案。"""
    pct = int(round(suggestion.confidence * 100))
    if applied and module_name:
        head = f"已自动归类到模块「{module_name}」（置信度 {pct}%）"
    elif module_name:
        head = f"建议归类到模块「{module_name}」（置信度 {pct}%，未自动落库）"
    elif suggestion.has_proposal:
        head = f"未匹配到已有模块，建议新建模块「{suggestion.proposed_name}」"
    else:
        head = f"未识别归属模块（最高置信度 {pct}%）"
    if suggestion.reasoning:
        return head + f"。理由：{suggestion.reasoning}"
    return head + "。"


async def _emit_module_classification(
    session_id: int,
    document_id: int,
    *,
    applied_module_id: int | None,
    module_name: str | None,
    suggestion: ModuleSuggestion,
):
    """统一发射「模块自动分类」的两帧（SSE 实时帧 + 落库的系统气泡）。

    三个上传处理器（PRD / lark / mindmap）此前各自重复这段，现集中于此。
    applied = 高置信度已落库；has_proposal = 建议新建模块（前端弹确认卡）。
    """
    applied = suggestion.is_high_confidence
    proposed = (
        {
            "name": suggestion.proposed_name,
            "code": suggestion.proposed_code,
            "description": suggestion.proposed_description,
        }
        if suggestion.has_proposal else None
    )
    yield _sse("module_auto_classified", {
        "document_id": document_id,
        "module_id": applied_module_id if applied else None,
        "module_name": module_name if applied else None,
        "proposed_module": proposed,
        "suggestion": {
            "module_id": suggestion.module_id,
            "confidence": suggestion.confidence,
            "reasoning": suggestion.reasoning,
            "applied": applied,
        },
    })
    classify_msg = await _write_assistant_message(
        session_id,
        _format_auto_classify_message(
            module_name=module_name,
            suggestion=suggestion,
            applied=applied,
        ),
        kind="module_auto_classified",
        ref={
            "document_id": document_id,
            "module_id": applied_module_id if applied else None,
            "suggested_module_id": suggestion.module_id,
            "confidence": suggestion.confidence,
            "applied": applied,
            "proposed_module": proposed,
        },
    )
    yield _sse("assistant_message", {"message": classify_msg})


async def _auto_classify_module_if_needed(
    *,
    project_id: int,
    user_module_id: int | None,
    raw_text: str,
) -> tuple[int | None, ModuleSuggestion | None]:
    """用户没显式指定模块时，调 LLM 给文档归类。

    返回 (final_module_id, suggestion)：
    - final_module_id：是 user_module_id（若已传）或高置信度自动归类结果（>=0.7），否则 None
    - suggestion：LLM 的原始建议（含中等置信结果），可能为 None（项目下无可选模块或文档为空）
                  调用方可把它通过 SSE 发给前端用作 "自动归类提示"
    """
    if user_module_id is not None:
        return user_module_id, None  # 用户已选，跳过分类
    if not (raw_text or "").strip():
        return None, None
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Module).where(Module.project_id == project_id)
            )).scalars().all()
        candidates = [
            {"id": m.id, "name": m.name, "description": m.description}
            for m in rows
        ]
    except Exception as exc:
        logger.warning("auto-classify: load modules failed: %s", exc)
        return None, None
    # 候选可以为空：classify_module 进入"纯提议"模式，让 LLM 直接建议一个新模块。
    suggestion = await classify_module(
        doc_content=truncate_for_llm(raw_text, limit=2000),
        candidates=candidates,
    )
    if suggestion.is_high_confidence:
        return suggestion.module_id, suggestion
    # 中置信建议 或 提议新模块 都把 suggestion 透传给调用方（SSE 发前端）
    has_signal = (suggestion.module_id is not None and suggestion.confidence > 0) or suggestion.has_proposal
    return None, suggestion if has_signal else None


async def _compute_knowledge_preview(
    *, project_id: int, module_id: int | None, document_id: int,
    raw_text: str, top_k: int | None = None,
) -> list[dict]:
    """跑一次 search_relevant 拿候选条目并补全字段；同文档抽取出的条目排除掉。

    失败一律返回 [] —— 知识检索是辅助，断网/无 embedding 都不该阻塞主流程。

    top_k None → 用 settings.knowledge_preview_top_k
    """
    if top_k is None:
        top_k = get_settings().knowledge_preview_top_k
    query = truncate_for_llm(raw_text or "", limit=2000)
    if not query.strip():
        return []
    try:
        async with AsyncSessionLocal() as db:
            hits = await search_relevant(
                db, project_id=project_id, module_id=module_id,
                query=query, top_k=top_k,
            )
            if not hits:
                return []
            hit_ids = [h.id for h in hits]
            rows = (await db.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.id.in_(hit_ids))
            )).scalars().all()
            by_id = {e.id: e for e in rows}
            dist_by_id = {h.id: h.distance for h in hits}
            return [
                _knowledge_hit_dict(by_id[h.id], dist_by_id.get(h.id))
                for h in hits
                # 排除来自同一文档的条目（自反馈污染防护）
                if h.id in by_id and by_id[h.id].document_id != document_id
            ]
    except Exception as exc:
        logger.warning("compute_knowledge_preview failed: %s", exc)
        return []


async def _resolve_knowledge_brief_by_ids(
    *, project_id: int, knowledge_ids: list[int] | None,
) -> str | None:
    """澄清/生成阶段用：按用户在前端勾选的 ids 拉条目并拼成 prompt 片段。
    None → 调用方该走自动 top-K；[] → 显式不注入。
    """
    if knowledge_ids is None or len(knowledge_ids) == 0:
        return None
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(KnowledgeEntry).where(
                    KnowledgeEntry.id.in_(knowledge_ids),
                    KnowledgeEntry.project_id == project_id,
                )
            )).scalars().all()
        if not rows:
            return None
        faux_hits = [
            HitEntry(
                id=e.id, knowledge_type=e.knowledge_type, content=e.content,
                confidence=float(e.confidence or 0.0), distance=None,
            )
            for e in rows
        ]
        return summarize_for_prompt(faux_hits) or None
    except Exception as exc:
        logger.warning("resolve knowledge by ids failed: %s", exc)
        return None

router = APIRouter()
logger = logging.getLogger("testcraft.upload")

MAX_CLARIFICATION_ROUNDS = 5


def _validate_upload(file: UploadFile) -> tuple[str, str]:
    allowed_types = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/pdf",
    }
    content_type = file.content_type or ""
    filename = file.filename or ""
    if not (
        content_type in allowed_types
        or filename.endswith(".docx")
        or filename.endswith(".pdf")
    ):
        raise HTTPException(
            status_code=400,
            detail="Only .docx and .pdf files are supported.",
        )
    return filename, content_type


def _sse(event: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event, **data}, ensure_ascii=False)}\n\n"


# ── Persistence helpers (Message + ClarificationState) ───────────────────────

def _msg_payload(msg: Message) -> dict:
    """把刚写入的 Message 行转成给前端 onAssistantMessage 用的 dict。"""
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


async def _verify_session(session_id: int, project_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(Session).where(Session.id == session_id, Session.project_id == project_id)
        )
        return r.scalar_one_or_none() is not None


async def _write_assistant_message(
    session_id: int, content: str, kind: str, ref: dict | None = None,
) -> dict:
    """落库一条系统气泡消息，返回 SSE 帧用的 dict。"""
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
        return _msg_payload(msg)


async def _extract_and_stash_pending(
    *, document_id: int, raw_text: str, module_name: str | None,
    project_id: int | None = None, module_id: int | None = None,
) -> list[dict]:
    """同步抽取产品知识草稿 → 写入 Document.pending_knowledge → 返回 drafts dict 列表。

    这里改成同步等待 LLM 完成（以前是 asyncio.create_task），原因是要让用户在澄清之前
    先审核草稿；草稿没出来就放行澄清等于把 "用户确认入库" 这一闸门变成空操作。
    LLM 抽取慢/超时时返回 []，前端按 "无草稿" 处理直接进入澄清。

    返回的 dict shape:
        {"knowledge_type": str, "content": str, "source": str, "confidence": float,
         "conflicts": list[{entry_id, knowledge_type, content, confidence, distance}]}
    `conflicts` 是同项目（含本模块或项目级）下与本草稿语义近似的已有条目——前端高亮提示
    用户："是更新旧条目还是两条都保留？"
    永远写一次 pending_knowledge（哪怕是 []）——前端用空数组判定 "已尝试且没结果"。

    project_id / module_id 用于查找潜在冲突；老调用站点没传时跳过冲突检测。
    """
    drafts_objs: list = []
    try:
        drafts_objs = await extract_knowledge(
            truncate_for_llm(raw_text), module_name=module_name,
        )
    except Exception as exc:
        logger.warning("knowledge extraction failed (treated as empty): %s", exc)
        drafts_objs = []

    drafts_dicts: list[dict] = []
    # 给每条 draft 找近邻（潜在冲突）；DB 错误整体 fail-open 不影响主流程
    for d in drafts_objs:
        item: dict = {
            "knowledge_type": d.knowledge_type,
            "content": d.content,
            "source": d.source,
            "confidence": float(d.confidence),
            "conflicts": [],
        }
        if project_id is not None:
            try:
                async with AsyncSessionLocal() as cdb:
                    sims = await find_similar_entries(
                        cdb, project_id=project_id, module_id=module_id,
                        content=d.content,
                    )
                    item["conflicts"] = [
                        {
                            "entry_id": s.entry_id,
                            "knowledge_type": s.knowledge_type,
                            "content": s.content,
                            "confidence": s.confidence,
                            "distance": round(s.distance, 4),
                        }
                        for s in sims
                    ]
            except Exception as exc:
                logger.warning("conflict scan failed for one draft: %s", exc)
                item["conflicts"] = []
        drafts_dicts.append(item)

    # 写入 Document.pending_knowledge —— 即使是 [] 也写，避免前端无法区分
    # "尚未抽取完" 与 "抽完了但啥也没出"
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Document).where(Document.id == document_id))
            doc = r.scalar_one_or_none()
            if doc is not None:
                doc.pending_knowledge = drafts_dicts
                await db.commit()
    except Exception as exc:
        logger.warning("persist pending_knowledge failed: %s", exc)

    return drafts_dicts


async def _extract_and_store_knowledge_bg(
    *, project_id: int, module_id: int | None, document_id: int,
    raw_text: str, module_name: str | None,
):
    """Legacy 后台抽取入口：保留兼容旧 import（routes 之外的代码若有引用）。
    新代码请改用 _extract_and_stash_pending —— 抽取的结果应当先经用户确认才入库。
    """
    try:
        drafts = await extract_knowledge(truncate_for_llm(raw_text), module_name=module_name)
        if not drafts:
            return
        async with AsyncSessionLocal() as db:
            n = await store_entries(
                db, project_id=project_id, module_id=module_id,
                document_id=document_id, drafts=drafts,
            )
            logger.info(
                "knowledge stored | project=%d module_id=%s document=%d count=%d",
                project_id, module_id, document_id, n,
            )
    except Exception as exc:
        logger.warning("knowledge extraction background task failed: %s", exc)


async def _upsert_clarification_state(
    session_id: int, project_id: int, **fields,
) -> None:
    """以 session_id 为主键 upsert ClarificationState，仅写非 None 字段。"""
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(ClarificationState).where(ClarificationState.session_id == session_id)
        )
        st = r.scalar_one_or_none()
        if st is None:
            st = ClarificationState(session_id=session_id, project_id=project_id)
            db.add(st)
        for k, v in fields.items():
            if v is not None:
                setattr(st, k, v)
        await db.commit()


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    module_id: int | None = Form(default=None),
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a Word/PDF document.
    Returns parsed content + clarification questions generated by the Clarifier Agent.
    """
    filename, _ = _validate_upload(file)

    file_bytes = await file.read()
    if len(file_bytes) > 20 * 1024 * 1024:  # 20 MB limit
        raise HTTPException(status_code=400, detail="File too large (max 20 MB).")

    logger.info("Upload received | filename=%s size=%d bytes module_id=%s", filename, len(file_bytes), module_id)

    # Parse document
    parse_start = time.perf_counter()
    try:
        parsed = parse_document(filename, file_bytes)
    except Exception as exc:
        logger.exception("Document parse failed | filename=%s", filename)
        raise HTTPException(status_code=422, detail=f"Failed to parse document: {exc}")
    parse_ms = (time.perf_counter() - parse_start) * 1000
    logger.info(
        "Document parsed | chunks=%d tables=%d raw_text_len=%d (%.0fms)",
        len(parsed["chunks"]),
        len(parsed["tables"]),
        len(parsed["raw_text"]),
        parse_ms,
    )

    # Resolve module name (scoped to this project)
    module_name = None
    if module_id:
        result = await db.execute(
            select(Module).where(Module.id == module_id, Module.project_id == project_id)
        )
        module = result.scalar_one_or_none()
        module_name = module.name if module else None

    # Persist document record
    ext = filename.rsplit(".", 1)[-1].lower()
    doc_record = Document(
        project_id=project_id,
        filename=filename,
        module_id=module_id,
        file_type=ext,
        parsed_content=parsed["chunks"],
        raw_text=parsed["raw_text"],
    )
    db.add(doc_record)
    await db.commit()
    await db.refresh(doc_record)

    logger.info("Document persisted | document_id=%d", doc_record.id)

    # Run Clarifier Agent
    existing_knowledge = await _fetch_knowledge_brief(
        project_id=project_id, module_id=module_id,
        query=truncate_for_llm(parsed["raw_text"], limit=2000),
    )
    clarify_start = time.perf_counter()
    clarification = await analyze_document_for_clarification(
        doc_content=truncate_for_llm(parsed["raw_text"]),
        module_name=module_name,
        existing_knowledge=existing_knowledge,
        system_prompt=await get_active_prompt_text(db, project_id, "clarifier_initial"),
    )
    clarify_ms = (time.perf_counter() - clarify_start) * 1000
    logger.info(
        "Clarifier finished | questions=%d ready_to_generate=%s (%.0fms)",
        len(clarification.get("questions", [])),
        clarification.get("ready_to_generate"),
        clarify_ms,
    )

    return {
        "document_id": doc_record.id,
        "filename": filename,
        "module_id": module_id,
        "stats": {
            "chunks": len(parsed["chunks"]),
            "tables": len(parsed["tables"]),
            "raw_text_length": len(parsed["raw_text"]),
        },
        "clarification": clarification,
    }


def _build_upload_done_content(filename: str, stats: dict, clarification: dict, *, cached: bool = False) -> str:
    summary = clarification.get("summary") or ""
    qcount = len(clarification.get("questions") or [])
    prefix = "命中缓存：" if cached else "已上传文档"
    head = (
        f"{prefix}《{filename}》（{stats['chunks']} 段 / {stats['tables']} 表 / {stats['raw_text_length']} 字）。"
    )
    body = ""
    if summary:
        body += f"\n\n**摘要：**{summary}"
    if qcount:
        body += f"\n\n第 1 轮识别到 **{qcount}** 个澄清问题（最多 {MAX_CLARIFICATION_ROUNDS} 轮）。"
    else:
        body += "\n\n本文档无遗留疑点，可直接生成用例。"
    return head + body


def _build_lark_done_content(filename: str, raw_text_len: int, clarification: dict, *, cached: bool = False) -> str:
    summary = clarification.get("summary") or ""
    qcount = len(clarification.get("questions") or [])
    prefix = "命中缓存：" if cached else "已从飞书导入"
    head = f"{prefix}《{filename}》（{raw_text_len} 字）。"
    body = ""
    if summary:
        body += f"\n\n**摘要：**{summary}"
    if qcount:
        body += f"\n\n第 1 轮识别到 **{qcount}** 个澄清问题（最多 {MAX_CLARIFICATION_ROUNDS} 轮）。"
    else:
        body += "\n\n本文档无遗留疑点，可直接生成用例。"
    return head + body


@router.post("/upload/stream")
async def upload_document_stream(
    file: UploadFile = File(...),
    session_id: int = Form(...),
    module_id: int | None = Form(default=None),
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
):
    import sys
    print(f"[DEBUG] /upload/stream entered file={file.filename} sid={session_id} pid={project_id}", flush=True, file=sys.stderr)
    """
    Stream upload progress as SSE events. New: requires session_id; persists
    a system Message and a ClarificationState row so refresh resumes seamlessly.
    """
    filename, _ = _validate_upload(file)
    print(f"[DEBUG] step1 validated filename={filename}", flush=True, file=sys.stderr)
    file_bytes = await file.read()
    print(f"[DEBUG] step2 file read bytes={len(file_bytes)}", flush=True, file=sys.stderr)
    if len(file_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 20 MB).")

    if not await _verify_session(session_id, project_id):
        raise HTTPException(status_code=404, detail="Session not found")
    print(f"[DEBUG] step3 session verified", flush=True, file=sys.stderr)

    logger.info("Upload(stream) received | filename=%s size=%d session_id=%s", filename, len(file_bytes), session_id)

    sha256 = hashlib.sha256(file_bytes).hexdigest()
    print(f"[DEBUG] step4 sha256={sha256[:12]}", flush=True, file=sys.stderr)

    async def events():
        async def emit_error(message: str):
            await _upsert_clarification_state(session_id, project_id, status="error")
            msg = await _write_assistant_message(
                session_id, f"❌ 上传失败：{message}", kind="upload_error",
                ref={"filename": filename},
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("error", {"message": message})

        # Stage 0: check for an identical document we've already processed.
        yield _sse("stage", {"stage": "fingerprinting", "message": f"计算内容指纹 sha256={sha256[:12]}…"})
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Document).where(Document.sha256 == sha256, Document.project_id == project_id)
            )
            existing = result.scalar_one_or_none()

        if existing and existing.clarification:
            logger.info(
                "Upload(stream) cache hit | sha=%s document_id=%d filename=%s",
                sha256[:12], existing.id, existing.filename,
            )
            stats = {
                "chunks": len(existing.parsed_content or []),
                "tables": 0,  # tables aren't persisted yet
                "raw_text_length": len(existing.raw_text or ""),
            }
            yield _sse("stage", {
                "stage": "cache_hit",
                "message": f"识别到同一份文档（曾以《{existing.filename}》上传过），复用上次的解析与澄清结果，跳过大模型调用。",
                "document_id": existing.id,
                "stats": stats,
            })
            clarification = existing.clarification
            content = _build_upload_done_content(existing.filename, stats, clarification, cached=True)
            await _upsert_clarification_state(
                session_id, project_id,
                document_id=existing.id,
                summary=clarification.get("summary"),
                module_detected=clarification.get("module_detected"),
                case_prefix_suggestion=clarification.get("case_prefix_suggestion"),
                current_round=1,
                rounds=[],
                current_questions=clarification.get("questions") or [],
                ready_to_generate=bool(clarification.get("ready_to_generate")),
                status="awaiting_answers" if clarification.get("questions") else "generating",
            )
            msg = await _write_assistant_message(
                session_id, content, kind="upload_done",
                ref={"document_id": existing.id, "cached": True},
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("result", {
                "document_id": existing.id,
                "filename": existing.filename,
                "module_id": existing.module_id,
                "stats": stats,
                "clarification": clarification,
                "cached": True,
            })
            yield _sse("done", {})
            return

        # Stage 1: parse
        yield _sse("stage", {"stage": "parsing", "message": f"正在解析《{filename}》…"})
        parse_start = time.perf_counter()
        try:
            parsed = parse_document(filename, file_bytes)
        except Exception as exc:
            logger.exception("Document parse failed | filename=%s", filename)
            async for ev in emit_error(f"文档解析失败：{exc}"):
                yield ev
            return
        parse_ms = (time.perf_counter() - parse_start) * 1000
        logger.info(
            "Document parsed(stream) | chunks=%d tables=%d raw_text_len=%d (%.0fms)",
            len(parsed["chunks"]), len(parsed["tables"]), len(parsed["raw_text"]), parse_ms,
        )
        yield _sse("stage", {
            "stage": "parsed",
            "message": f"解析完成：{len(parsed['chunks'])} 段 / {len(parsed['tables'])} 表 / {len(parsed['raw_text'])} 字（{parse_ms:.0f}ms）",
            "stats": {
                "chunks": len(parsed["chunks"]),
                "tables": len(parsed["tables"]),
                "raw_text_length": len(parsed["raw_text"]),
            },
        })

        # Stage 2: resolve module（用户没选时尝试自动分类）
        module_name: str | None = None
        auto_module_suggestion: ModuleSuggestion | None = None
        effective_module_id = module_id
        if effective_module_id is None:
            yield _sse("stage", {"stage": "module_classify", "message": "正在分析文档归属模块…"})
            classified_id, auto_module_suggestion = await _auto_classify_module_if_needed(
                project_id=project_id,
                user_module_id=None,
                raw_text=parsed["raw_text"],
            )
            if classified_id is not None:
                effective_module_id = classified_id
                logger.info(
                    "auto-classify hit | document module_id=%s confidence=%.2f",
                    classified_id,
                    auto_module_suggestion.confidence if auto_module_suggestion else 0.0,
                )
        async with AsyncSessionLocal() as db:
            if effective_module_id:
                result = await db.execute(
                    select(Module).where(
                        Module.id == effective_module_id,
                        Module.project_id == project_id,
                    )
                )
                module = result.scalar_one_or_none()
                module_name = module.name if module else None

            ext = filename.rsplit(".", 1)[-1].lower()
            doc_record = Document(
                project_id=project_id,
                filename=filename,
                sha256=sha256,
                module_id=effective_module_id,
                file_type=ext,
                parsed_content=parsed["chunks"],
                raw_text=parsed["raw_text"],
            )
            db.add(doc_record)
            await db.commit()
            await db.refresh(doc_record)
            document_id = doc_record.id
        # 自动归类成功 → 推 SSE 让前端 chip 跟着切；中等置信度也推（前端弹"建议归类到 XX"）
        if auto_module_suggestion is not None:
            async for ev in _emit_module_classification(
                session_id, document_id,
                applied_module_id=effective_module_id,
                module_name=module_name,
                suggestion=auto_module_suggestion,
            ):
                yield ev
        # 下游统一用 effective_module_id（包含自动归类结果）；不要再重新赋值 module_id，
        # 否则在 events() 闭包里 module_id 会被 Python 视作 local 变量 → 上面 effective_module_id = module_id 触发 UnboundLocalError
        logger.info("Document persisted(stream) | document_id=%d", document_id)
        yield _sse("stage", {"stage": "persisted", "document_id": document_id, "message": "文档已入库"})

        # B 方案：在跑 Clarifier 之前停下来，让用户先确认要注入到澄清提示词里的知识库条目。
        # 这里只算预览（不写 Document.clarification、不调 LLM），把候选条目通过 SSE 推给前端。
        # 真正的 Clarifier 由前端在 /api/clarify/initial/stream 触发，带上用户勾选的 ids。
        stats = {
            "chunks": len(parsed["chunks"]),
            "tables": len(parsed["tables"]),
            "raw_text_length": len(parsed["raw_text"]),
        }
        yield _sse("stage", {"stage": "knowledge_lookup", "message": "正在检索项目知识库相关条目…"})
        knowledge_hits = await _compute_knowledge_preview(
            project_id=project_id, module_id=effective_module_id, document_id=document_id,
            raw_text=parsed["raw_text"],
        )
        await _upsert_clarification_state(
            session_id, project_id,
            document_id=document_id,
            module_detected=module_name,
            current_round=1,
            rounds=[],
            current_questions=[],
            ready_to_generate=False,
            status="awaiting_clarification",
        )
        yield _sse("knowledge_preview", {
            "document_id": document_id,
            "module_id": effective_module_id,
            "filename": filename,
            "stats": stats,
            "module_name": module_name,
            "hits": knowledge_hits,
        })

        # 同步抽取产品知识草稿（B 方案"用户确认入库"闸门）：抽取完成后才发 done
        # —— 让前端在进入澄清前先弹出草稿审核面板。
        yield _sse("stage", {"stage": "knowledge_extract", "message": "正在抽取可沉淀的产品知识…"})
        drafts = await _extract_and_stash_pending(
            document_id=document_id,
            raw_text=parsed["raw_text"],
            module_name=module_name,
            project_id=project_id,
            module_id=effective_module_id,
        )
        yield _sse("extracted_knowledge_drafts", {
            "document_id": document_id,
            "module_id": effective_module_id,
            "module_name": module_name,
            "role": "prd",
            "drafts": drafts,
        })
        yield _sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


class LarkImportRequest(BaseModel):
    url: str
    session_id: int
    module_id: int | None = None
    # 飞书文档的角色：prd（默认，老路径，写 Document.role='prd' + ClarificationState.document_id）
    # 或 mindmap（写 Document.role='mindmap' + ClarificationState.mindmap_document_id，
    # 且用 mindmap_parser 重新解析 raw_text 把它当 markdown 大纲展开成层级 chunks）。
    role: str = "prd"


@router.post("/upload/lark/stream")
async def upload_lark_stream(
    req: LarkImportRequest,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
):
    """Import a Feishu/Lark document by URL via local lark-cli.

    Same SSE event shape as /api/upload/stream, plus assistant_message frames
    so refresh can replay system bubbles from DB.
    """
    url = (req.url or "").strip()
    module_id = req.module_id
    session_id = req.session_id
    role = (req.role or "prd").lower()
    if role not in ("prd", "mindmap"):
        raise HTTPException(status_code=400, detail="role 仅支持 prd / mindmap。")
    is_mindmap_role = role == "mindmap"
    logger.info(
        "Lark import received | url=%s module_id=%s session_id=%s role=%s",
        url[:200], module_id, session_id, role,
    )

    if not await _verify_session(session_id, project_id):
        raise HTTPException(status_code=404, detail="Session not found")

    async def events():
        # module_id 在下面（自动归类命中时）会被重新赋值；不声明 nonlocal 的话
        # Python 会把它当 events() 的局部变量，导致赋值前的读取触发 UnboundLocalError。
        nonlocal module_id

        async def emit_error(message: str):
            await _upsert_clarification_state(session_id, project_id, status="error")
            err_kind = "mindmap_error" if is_mindmap_role else "lark_error"
            err_prefix = "❌ 飞书脑图导入失败：" if is_mindmap_role else "❌ 飞书导入失败："
            msg = await _write_assistant_message(
                session_id, f"{err_prefix}{message}", kind=err_kind,
                ref={"url": url[:200], "role": role},
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("error", {"message": message})

        # Stage 1: 校验 URL 形态（不打 lark-cli，纯本地正则）
        yield _sse("stage", {"stage": "validating", "message": "校验飞书链接…"})
        kind = classify_lark_url(url)
        if kind == "unknown":
            async for ev in emit_error("不是合法的飞书文档链接（仅支持 docx / wiki / docs / sheets 路径）。"):
                yield ev
            return
        if kind == "sheet":
            async for ev in emit_error("飞书电子表格暂不支持，请将表格导出为 xlsx 后再上传。"):
                yield ev
            return

        # Stage 2: 调 lark-cli 抓正文
        yield _sse("stage", {
            "stage": "fetching",
            "message": f"正在通过 lark-cli 抓取飞书{ {'docx': '新版文档', 'wiki': '知识库', 'docs': '旧版文档'}.get(kind, '文档') }…",
        })
        try:
            content = await fetch_lark_content(url)
        except (LarkUrlInvalid, LarkSheetUnsupported, LarkCliNotLoggedIn,
                LarkPermissionDenied, LarkFetchTimeout, LarkEmptyDoc) as exc:
            async for ev in emit_error(str(exc)):
                yield ev
            return
        except LarkCliNotInstalled as exc:
            logger.exception("lark-cli not installed")
            async for ev in emit_error(f"lark-cli 未安装：{exc}"):
                yield ev
            return
        except LarkFetchError as exc:
            logger.exception("Lark fetch failed | url=%s", url[:200])
            async for ev in emit_error(f"飞书抓取失败：{exc}"):
                yield ev
            return
        except Exception as exc:
            logger.exception("Lark fetch unknown error | url=%s", url[:200])
            async for ev in emit_error(f"飞书抓取异常：{exc}"):
                yield ev
            return

        raw_text = content.raw_text
        title = content.title
        file_type = f"lark_{kind}"
        logger.info(
            "Lark fetched | kind=%s title=%s raw_text_len=%d",
            kind, title[:60], len(raw_text),
        )

        # Stage 3: fingerprint —— 与 file 路径同一缓存机制（按 raw_text 哈希）
        sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        yield _sse("stage", {"stage": "fingerprinting", "message": f"计算内容指纹 sha256={sha256[:12]}…"})

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Document).where(Document.sha256 == sha256, Document.project_id == project_id)
            )
            existing = result.scalar_one_or_none()

        if existing and (is_mindmap_role or existing.clarification):
            # PRD 路径要求 existing.clarification 非空（保持原行为，复用澄清缓存）
            # 脑图路径不需要 clarification 缓存，命中即复用 Document 行
            stats = {
                "chunks": len(existing.parsed_content or []),
                "tables": 0,
                "raw_text_length": len(existing.raw_text or ""),
            }
            cache_hit_msg = (
                f"识别到同一份脑图（曾以《{existing.filename}》导入过），复用解析结果。"
                if is_mindmap_role else
                f"识别到同一份文档（曾以《{existing.filename}》导入过），复用上次的解析与澄清结果，跳过大模型调用。"
            )
            yield _sse("stage", {
                "stage": "cache_hit",
                "message": cache_hit_msg,
                "document_id": existing.id,
                "stats": stats,
            })
            if is_mindmap_role:
                # 脑图缓存命中：只 upsert mindmap_document_id，不动 PRD slot / 澄清状态
                await _upsert_clarification_state(
                    session_id, project_id,
                    mindmap_document_id=existing.id,
                )
                msg = await _write_assistant_message(
                    session_id, _build_mindmap_done_content(existing.filename, stats),
                    kind="mindmap_done",
                    ref={
                        "document_id": existing.id, "url": url[:200],
                        "cached": True, "role": "mindmap", "source": "lark",
                    },
                )
                yield _sse("assistant_message", {"message": msg})
                yield _sse("knowledge_preview", {
                    "document_id": existing.id,
                    "module_id": existing.module_id,
                    "filename": existing.filename,
                    "stats": stats,
                    "module_name": None,
                    "hits": [],
                    "role": "mindmap",
                    "source": "lark",
                    "url": url[:200],
                    "cached": True,
                })
                yield _sse("done", {})
                return
            clarification = existing.clarification
            await _upsert_clarification_state(
                session_id, project_id,
                document_id=existing.id,
                summary=clarification.get("summary"),
                module_detected=clarification.get("module_detected"),
                case_prefix_suggestion=clarification.get("case_prefix_suggestion"),
                current_round=1,
                rounds=[],
                current_questions=clarification.get("questions") or [],
                ready_to_generate=bool(clarification.get("ready_to_generate")),
                status="awaiting_answers" if clarification.get("questions") else "generating",
            )
            msg = await _write_assistant_message(
                session_id,
                _build_lark_done_content(existing.filename, len(existing.raw_text or ""), clarification, cached=True),
                kind="lark_done",
                ref={"document_id": existing.id, "url": url[:200], "cached": True},
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("result", {
                "document_id": existing.id,
                "filename": existing.filename,
                "module_id": existing.module_id,
                "stats": stats,
                "clarification": clarification,
                "cached": True,
            })
            yield _sse("done", {})
            return

        # Stage 4: persist Document（source_type=lark）
        # 用户没指定 module 时尝试自动归类（脑图 raw_text 和 PRD 同样是有效信号）
        auto_module_suggestion: ModuleSuggestion | None = None
        if module_id is None:
            yield _sse("stage", {"stage": "module_classify", "message": "正在分析文档归属模块…"})
            classified_id, auto_module_suggestion = await _auto_classify_module_if_needed(
                project_id=project_id,
                user_module_id=None,
                raw_text=raw_text,
            )
            if classified_id is not None:
                module_id = classified_id
        async with AsyncSessionLocal() as db:
            module_name: str | None = None
            if module_id:
                result = await db.execute(
                    select(Module).where(Module.id == module_id, Module.project_id == project_id)
                )
                module = result.scalar_one_or_none()
                module_name = module.name if module else None

            if is_mindmap_role:
                # 脑图：把 lark 抓回来的 markdown 文本喂给 mindmap_parser 拆成层级 chunks，
                # raw_text 复用 parser 的输出（标准化缩进），与本地 .md 上传路径同 shape。
                try:
                    parsed_mm = parse_mindmap_md(raw_text.encode("utf-8"))
                except Exception as exc:
                    logger.exception("Lark mindmap parse failed | url=%s", url[:200])
                    async for ev in emit_error(f"飞书脑图解析失败：{exc}"):
                        yield ev
                    return
                doc_record = Document(
                    project_id=project_id,
                    filename=title,
                    sha256=sha256,
                    module_id=module_id,
                    file_type="mindmap_md",
                    role="mindmap",
                    source_type="lark",
                    source_url=url,
                    parsed_content=parsed_mm["chunks"],
                    raw_text=parsed_mm["raw_text"],
                )
            else:
                doc_record = Document(
                    project_id=project_id,
                    filename=title,
                    sha256=sha256,
                    module_id=module_id,
                    file_type=file_type,
                    source_type="lark",
                    source_url=url,
                    # 占位 chunks，保持与 file 来源同 shape
                    parsed_content=[{"type": "markdown", "text": raw_text}],
                    raw_text=raw_text,
                )
            db.add(doc_record)
            await db.commit()
            await db.refresh(doc_record)
            document_id = doc_record.id

        if is_mindmap_role:
            stats = {
                "chunks": len(doc_record.parsed_content or []),
                "tables": 0,
                "raw_text_length": len(doc_record.raw_text or ""),
            }
        else:
            stats = {
                "chunks": 1,
                "tables": 0,
                "raw_text_length": len(raw_text),
            }
        logger.info(
            "Lark document persisted | document_id=%d title=%s role=%s",
            document_id, title[:60], role,
        )
        if auto_module_suggestion is not None:
            async for ev in _emit_module_classification(
                session_id, document_id,
                applied_module_id=module_id,
                module_name=module_name,
                suggestion=auto_module_suggestion,
            ):
                yield ev
        yield _sse("stage", {"stage": "persisted", "document_id": document_id, "message": "文档已入库"})

        # 知识库预览 + 状态写库 + 系统气泡
        yield _sse("stage", {"stage": "knowledge_lookup", "message": "正在检索项目知识库相关条目…"})
        knowledge_hits = await _compute_knowledge_preview(
            project_id=project_id, module_id=module_id, document_id=document_id,
            raw_text=doc_record.raw_text or "",
        )

        if is_mindmap_role:
            # 脑图：只 upsert mindmap_document_id，不动 PRD slot / 澄清状态（与本地 .md 上传一致）
            await _upsert_clarification_state(
                session_id, project_id,
                mindmap_document_id=document_id,
            )
            msg = await _write_assistant_message(
                session_id, _build_mindmap_done_content(title, stats),
                kind="mindmap_done",
                ref={
                    "document_id": document_id, "url": url[:200],
                    "role": "mindmap", "source": "lark",
                },
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("knowledge_preview", {
                "document_id": document_id,
                "module_id": module_id,
                "filename": title,
                "stats": stats,
                "module_name": module_name,
                "hits": knowledge_hits,
                "role": "mindmap",
                "source": "lark",
                "url": url[:200],
            })

            # 知识抽取（同步等待，详见 _extract_and_stash_pending）
            yield _sse("stage", {"stage": "knowledge_extract", "message": "正在抽取可沉淀的产品知识…"})
            drafts = await _extract_and_stash_pending(
                document_id=document_id,
                raw_text=doc_record.raw_text or "",
                module_name=module_name,
                project_id=project_id,
                module_id=module_id,
            )
            yield _sse("extracted_knowledge_drafts", {
                "document_id": document_id,
                "module_id": module_id,
                "module_name": module_name,
                "role": "mindmap",
                "drafts": drafts,
            })
            yield _sse("done", {})
            return

        # PRD 路径（原行为）：B 方案先停在预览阶段，由前端确认后再调 /clarify/initial/stream
        await _upsert_clarification_state(
            session_id, project_id,
            document_id=document_id,
            module_detected=module_name,
            current_round=1,
            rounds=[],
            current_questions=[],
            ready_to_generate=False,
            status="awaiting_clarification",
        )
        yield _sse("knowledge_preview", {
            "document_id": document_id,
            "module_id": module_id,
            "filename": title,
            "stats": stats,
            "module_name": module_name,
            "hits": knowledge_hits,
            "source": "lark",
            "url": url[:200],
        })

        # 知识抽取（同步等待）
        yield _sse("stage", {"stage": "knowledge_extract", "message": "正在抽取可沉淀的产品知识…"})
        drafts = await _extract_and_stash_pending(
            document_id=document_id,
            raw_text=raw_text,
            module_name=module_name,
            project_id=project_id,
            module_id=module_id,
        )
        yield _sse("extracted_knowledge_drafts", {
            "document_id": document_id,
            "module_id": module_id,
            "module_name": module_name,
            "role": "prd",
            "source": "lark",
            "drafts": drafts,
        })
        yield _sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


class ClarificationRound(BaseModel):
    questions: list[dict] = []
    answers: dict[str, str] = {}


# ── Mindmap (.md) upload ─────────────────────────────────────────────────────

def _validate_mindmap_upload(file: UploadFile) -> str:
    """脑图仅接受 Markdown 大纲（.md）。其他格式（如 .xmind）后续 parser 扩展时再放开。"""
    filename = file.filename or ""
    if not filename.lower().endswith(".md"):
        raise HTTPException(
            status_code=400,
            detail="仅支持 .md 测试脑图（Markdown 大纲）。",
        )
    return filename


def _build_mindmap_done_content(filename: str, stats: dict) -> str:
    return (
        f"已上传测试脑图《{filename}》（{stats['chunks']} 个节点 / {stats['raw_text_length']} 字）。\n\n"
        "可继续上传 PRD 文档作为补充，或直接「开始澄清」让脑图作为唯一输入。"
    )


def _build_mindmap_clarify_done_content(filename: str, stats: dict, clarification: dict) -> str:
    """脑图作为唯一输入完成澄清后的系统气泡（区别于 PRD/飞书路径，不带"段/表"统计）。"""
    summary = clarification.get("summary") or ""
    qcount = len(clarification.get("questions") or [])
    head = f"已基于测试脑图《{filename}》（{stats['chunks']} 个节点 / {stats['raw_text_length']} 字）完成澄清。"
    body = ""
    if summary:
        body += f"\n\n**摘要：**{summary}"
    if qcount:
        body += f"\n\n第 1 轮识别到 **{qcount}** 个澄清问题（最多 {MAX_CLARIFICATION_ROUNDS} 轮）。"
    else:
        body += "\n\n本脑图无遗留疑点，可直接生成用例。"
    return head + body


@router.post("/upload/mindmap/stream")
async def upload_mindmap_stream(
    file: UploadFile = File(...),
    session_id: int = Form(...),
    module_id: int | None = Form(default=None),
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
):
    """
    Stream test mindmap (.md) upload progress as SSE events. Mirrors /upload/stream's
    event sequence (fingerprinting / parsed / persisted / knowledge_lookup /
    knowledge_preview), but persists Document(role="mindmap") and writes the id to
    ClarificationState.mindmap_document_id (without touching .document_id, which
    represents the PRD).
    """
    filename = _validate_mindmap_upload(file)
    file_bytes = await file.read()
    if len(file_bytes) > 5 * 1024 * 1024:  # 5 MB（脑图远小于 PRD）
        raise HTTPException(status_code=400, detail="脑图文件过大（最大 5 MB）。")

    if not await _verify_session(session_id, project_id):
        raise HTTPException(status_code=404, detail="Session not found")

    logger.info("Mindmap upload(stream) received | filename=%s size=%d session_id=%s",
                filename, len(file_bytes), session_id)

    sha256 = hashlib.sha256(file_bytes).hexdigest()

    async def events():
        async def emit_error(message: str):
            await _upsert_clarification_state(session_id, project_id, status="error")
            msg = await _write_assistant_message(
                session_id, f"❌ 脑图上传失败：{message}", kind="mindmap_error",
                ref={"filename": filename},
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("error", {"message": message})

        # Stage 0: 内容指纹 → 命中缓存就直接复用历史 Document（按项目 + sha256）
        yield _sse("stage", {"stage": "fingerprinting", "message": f"计算内容指纹 sha256={sha256[:12]}…"})
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(Document).where(Document.sha256 == sha256, Document.project_id == project_id)
            )
            existing = r.scalar_one_or_none()

        if existing:
            stats = {
                "chunks": len(existing.parsed_content or []),
                "tables": 0,
                "raw_text_length": len(existing.raw_text or ""),
            }
            yield _sse("stage", {
                "stage": "cache_hit",
                "message": f"识别到同一份脑图（曾以《{existing.filename}》上传过），复用解析结果。",
                "document_id": existing.id,
                "stats": stats,
            })
            await _upsert_clarification_state(
                session_id, project_id,
                mindmap_document_id=existing.id,
            )
            msg = await _write_assistant_message(
                session_id, _build_mindmap_done_content(existing.filename, stats),
                kind="mindmap_done",
                ref={"document_id": existing.id, "cached": True, "role": "mindmap"},
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("knowledge_preview", {
                "document_id": existing.id,
                "module_id": existing.module_id,
                "filename": existing.filename,
                "stats": stats,
                "module_name": None,
                "hits": [],
                "role": "mindmap",
                "cached": True,
            })
            yield _sse("done", {})
            return

        # Stage 1: parse markdown outline
        yield _sse("stage", {"stage": "parsing", "message": f"正在解析脑图《{filename}》…"})
        parse_start = time.perf_counter()
        try:
            parsed = parse_mindmap_md(file_bytes)
        except Exception as exc:
            logger.exception("Mindmap parse failed | filename=%s", filename)
            async for ev in emit_error(f"脑图解析失败：{exc}"):
                yield ev
            return
        parse_ms = (time.perf_counter() - parse_start) * 1000
        stats = {
            "chunks": len(parsed["chunks"]),
            "tables": 0,
            "raw_text_length": len(parsed["raw_text"]),
        }
        logger.info(
            "Mindmap parsed(stream) | nodes=%d raw_text_len=%d (%.0fms)",
            stats["chunks"], stats["raw_text_length"], parse_ms,
        )
        yield _sse("stage", {
            "stage": "parsed",
            "message": f"解析完成：{stats['chunks']} 个节点 / {stats['raw_text_length']} 字（{parse_ms:.0f}ms）",
            "stats": stats,
        })

        # Stage 2: persist Document(role="mindmap")（用户没指定模块时尝试自动归类）
        auto_module_suggestion: ModuleSuggestion | None = None
        if module_id is None:
            yield _sse("stage", {"stage": "module_classify", "message": "正在分析脑图归属模块…"})
            classified_id, auto_module_suggestion = await _auto_classify_module_if_needed(
                project_id=project_id,
                user_module_id=None,
                raw_text=parsed["raw_text"],
            )
            if classified_id is not None:
                module_id = classified_id
        async with AsyncSessionLocal() as db:
            module_name: str | None = None
            if module_id:
                mr = await db.execute(
                    select(Module).where(Module.id == module_id, Module.project_id == project_id)
                )
                module = mr.scalar_one_or_none()
                module_name = module.name if module else None

            doc_record = Document(
                project_id=project_id,
                filename=filename,
                sha256=sha256,
                module_id=module_id,
                file_type="mindmap_md",
                role="mindmap",
                parsed_content=parsed["chunks"],
                raw_text=parsed["raw_text"],
            )
            db.add(doc_record)
            await db.commit()
            await db.refresh(doc_record)
            document_id = doc_record.id
        logger.info("Mindmap persisted(stream) | document_id=%d", document_id)
        if auto_module_suggestion is not None:
            async for ev in _emit_module_classification(
                session_id, document_id,
                applied_module_id=module_id,
                module_name=module_name,
                suggestion=auto_module_suggestion,
            ):
                yield ev
        yield _sse("stage", {"stage": "persisted", "document_id": document_id, "message": "脑图已入库"})

        # Stage 3: 知识库预览 + 状态写库 + 系统气泡（与 /upload/stream 同节奏）
        yield _sse("stage", {"stage": "knowledge_lookup", "message": "正在检索项目知识库相关条目…"})
        knowledge_hits = await _compute_knowledge_preview(
            project_id=project_id, module_id=module_id, document_id=document_id,
            raw_text=parsed["raw_text"],
        )
        await _upsert_clarification_state(
            session_id, project_id,
            mindmap_document_id=document_id,
        )
        msg = await _write_assistant_message(
            session_id, _build_mindmap_done_content(filename, stats),
            kind="mindmap_done",
            ref={"document_id": document_id, "role": "mindmap"},
        )
        yield _sse("assistant_message", {"message": msg})
        yield _sse("knowledge_preview", {
            "document_id": document_id,
            "module_id": module_id,
            "filename": filename,
            "stats": stats,
            "module_name": module_name,
            "hits": knowledge_hits,
            "role": "mindmap",
        })

        # 知识抽取（同步等待）—— 脑图也是有效产品上下文
        yield _sse("stage", {"stage": "knowledge_extract", "message": "正在抽取可沉淀的产品知识…"})
        drafts = await _extract_and_stash_pending(
            document_id=document_id,
            raw_text=parsed["raw_text"],
            module_name=module_name,
            project_id=project_id,
            module_id=module_id,
        )
        yield _sse("extracted_knowledge_drafts", {
            "document_id": document_id,
            "module_id": module_id,
            "module_name": module_name,
            "role": "mindmap",
            "drafts": drafts,
        })
        yield _sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


class InitialClarifyRequest(BaseModel):
    session_id: int
    # PRD 与脑图至少需要其中之一非空（路由内做交叉校验）
    document_id: int | None = None
    mindmap_document_id: int | None = None
    # 用户在前端预览面板勾选的知识库条目 id：
    #   None  → 自动 top-K 检索（同 doc 排除）
    #   []    → 显式不注入
    #   非空  → 仅这些 ids
    knowledge_ids: list[int] | None = None


@router.post("/clarify/initial/stream")
async def clarify_initial_stream(
    req: InitialClarifyRequest,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
):
    """B 方案第二步：用户在 KnowledgePreviewPanel 勾选完知识条目后调它，
    才真正跑 Clarifier。事件序列与原 /upload/stream 末段一致：
    stage:clarifying → token* → assistant_message(upload_done | lark_done | mindmap_done) → result → done
    """
    session_id = req.session_id
    document_id = req.document_id
    mindmap_document_id = req.mindmap_document_id

    if document_id is None and mindmap_document_id is None:
        raise HTTPException(status_code=400, detail="至少需要提供 PRD 文档或测试脑图其中之一。")

    if not await _verify_session(session_id, project_id):
        raise HTTPException(status_code=404, detail="Session not found")

    async with AsyncSessionLocal() as db:
        prd_doc: Document | None = None
        mindmap_doc: Document | None = None
        if document_id is not None:
            r = await db.execute(
                select(Document).where(Document.id == document_id, Document.project_id == project_id)
            )
            prd_doc = r.scalar_one_or_none()
            if prd_doc is None:
                raise HTTPException(status_code=404, detail="Document not found")
        if mindmap_document_id is not None:
            r = await db.execute(
                select(Document).where(
                    Document.id == mindmap_document_id, Document.project_id == project_id
                )
            )
            mindmap_doc = r.scalar_one_or_none()
            if mindmap_doc is None:
                raise HTTPException(status_code=404, detail="Mindmap document not found")
        # 模块名解析顺序：PRD.module_id 优先，脑图 module_id 兜底
        module_obj: Module | None = None
        primary_module_id = (
            (prd_doc.module_id if prd_doc else None)
            or (mindmap_doc.module_id if mindmap_doc else None)
        )
        if primary_module_id:
            mr = await db.execute(
                select(Module).where(
                    Module.id == primary_module_id, Module.project_id == project_id
                )
            )
            module_obj = mr.scalar_one_or_none()

    module_name = module_obj.name if module_obj else None
    prd_raw_text = (prd_doc.raw_text or "") if prd_doc else ""
    mindmap_raw_text = (mindmap_doc.raw_text or "") if mindmap_doc else ""
    is_lark = bool(prd_doc and (prd_doc.source_type or "file") == "lark")
    has_mindmap = mindmap_doc is not None
    has_prd = prd_doc is not None

    prd_filename = prd_doc.filename if prd_doc else None
    mindmap_filename = mindmap_doc.filename if mindmap_doc else None
    prd_stats = (
        {
            "chunks": len(prd_doc.parsed_content or []),
            "tables": 0,
            "raw_text_length": len(prd_raw_text),
        }
        if prd_doc else None
    )
    mindmap_stats = (
        {
            "chunks": len(mindmap_doc.parsed_content or []),
            "tables": 0,
            "raw_text_length": len(mindmap_raw_text),
        }
        if mindmap_doc else None
    )

    async def events():
        async def emit_error(message: str):
            await _upsert_clarification_state(session_id, project_id, status="error")
            if has_mindmap and not has_prd:
                kind = "mindmap_error"
            elif is_lark:
                kind = "lark_error"
            else:
                kind = "upload_error"
            err_msg = await _write_assistant_message(
                session_id, f"❌ 澄清分析失败：{message}", kind=kind,
                ref={"document_id": document_id, "mindmap_document_id": mindmap_document_id},
            )
            yield _sse("assistant_message", {"message": err_msg})
            yield _sse("error", {"message": message})

        yield _sse("stage", {"stage": "clarifying", "message": "正在调用大模型识别歧义点…"})

        # 把用户在 KnowledgePreviewPanel 上的选择作为一条系统气泡先落库 + 推回前端，
        # 这样"确认 N 条"这一步会在聊天流里留痕，而不是面板一消失就什么都不剩。
        sel_msg = await record_knowledge_selection(
            session_id=session_id, project_id=project_id, phase="clarify",
            knowledge_ids=req.knowledge_ids,
        )
        if sel_msg is not None:
            yield _sse("assistant_message", {"message": sel_msg})

        # 知识库 query：脑图代表测试人员最终意图，更贴近"我们要测什么"，优先用脑图原文；
        # 仅 PRD 时退回 PRD 原文；同文档排除取 PRD（如有）以避免自反馈污染（脑图本身入库较少）。
        knowledge_query_src = mindmap_raw_text or prd_raw_text
        knowledge_module_id = (
            (prd_doc.module_id if prd_doc else None)
            or (mindmap_doc.module_id if mindmap_doc else None)
        )
        if req.knowledge_ids is None:
            existing_knowledge = await _fetch_knowledge_brief(
                project_id=project_id,
                module_id=knowledge_module_id,
                query=truncate_for_llm(knowledge_query_src, limit=2000),
                document_id=(prd_doc.id if prd_doc else (mindmap_doc.id if mindmap_doc else None)),
            )
        else:
            existing_knowledge = await _resolve_knowledge_brief_by_ids(
                project_id=project_id, knowledge_ids=req.knowledge_ids,
            )

        clarification: dict | None = None
        clarify_start = time.perf_counter()
        try:
            async for kind_event, payload in stream_analyze_document_for_clarification(
                doc_content=truncate_for_llm(prd_raw_text) if prd_raw_text else "",
                module_name=module_name,
                existing_knowledge=existing_knowledge,
                mindmap_content=truncate_for_llm(mindmap_raw_text) if mindmap_raw_text else None,
                system_prompt=await resolve_active_prompt(project_id, "clarifier_initial"),
            ):
                if kind_event == "token":
                    yield _sse("token", {"content": payload})
                elif kind_event == "result":
                    clarification = payload
        except Exception as exc:
            logger.exception(
                "Initial clarifier stream failed | document_id=%s mindmap=%s",
                document_id, mindmap_document_id,
            )
            async for ev in emit_error(str(exc)):
                yield ev
            return
        clarify_ms = (time.perf_counter() - clarify_start) * 1000

        if clarification is None:
            clarification = {
                "summary": "文档解析完成",
                "module_detected": module_name or "未知模块",
                "questions": [],
                "ready_to_generate": True,
            }

        logger.info(
            "Initial clarifier(stream) finished | prd=%s mindmap=%s questions=%d ready=%s (%.0fms)",
            document_id, mindmap_document_id,
            len(clarification.get("questions", [])),
            clarification.get("ready_to_generate"),
            clarify_ms,
        )

        # 缓存澄清结果到 Document：仅当"PRD-only 且无脑图"时复用，避免组合输入污染缓存
        if prd_doc and not has_mindmap:
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Document).where(Document.id == prd_doc.id))
                d = r.scalar_one_or_none()
                if d:
                    d.clarification = clarification
                    await db.commit()

        await _upsert_clarification_state(
            session_id, project_id,
            document_id=document_id,
            mindmap_document_id=mindmap_document_id,
            summary=clarification.get("summary"),
            module_detected=clarification.get("module_detected"),
            case_prefix_suggestion=clarification.get("case_prefix_suggestion"),
            current_round=1,
            rounds=[],
            current_questions=clarification.get("questions") or [],
            ready_to_generate=bool(clarification.get("ready_to_generate")),
            status="awaiting_answers" if clarification.get("questions") else "generating",
        )

        # 选系统气泡内容：PRD 在场则按 PRD 老逻辑（飞书 vs 普通 docx 分支），仅脑图用脑图气泡
        if has_prd:
            if is_lark:
                content = _build_lark_done_content(prd_filename, len(prd_raw_text), clarification)
                kind_label = "lark_done"
                ref = {
                    "document_id": document_id,
                    "url": prd_doc.source_url or None,
                    "mindmap_document_id": mindmap_document_id,
                }
            else:
                content = _build_upload_done_content(prd_filename, prd_stats, clarification)
                kind_label = "upload_done"
                ref = {
                    "document_id": document_id,
                    "mindmap_document_id": mindmap_document_id,
                }
        else:
            content = _build_mindmap_clarify_done_content(mindmap_filename, mindmap_stats, clarification)
            kind_label = "mindmap_clarify_done"
            ref = {"mindmap_document_id": mindmap_document_id}
        msg = await _write_assistant_message(session_id, content, kind=kind_label, ref=ref)
        yield _sse("assistant_message", {"message": msg})

        yield _sse("result", {
            "document_id": document_id,
            "mindmap_document_id": mindmap_document_id,
            "filename": prd_filename or mindmap_filename,
            "module_id": (prd_doc.module_id if prd_doc else (mindmap_doc.module_id if mindmap_doc else None)),
            "stats": prd_stats or mindmap_stats,
            "clarification": clarification,
        })
        yield _sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


class FollowupRequest(BaseModel):
    document_id: int | None = None
    mindmap_document_id: int | None = None
    session_id: int
    module_name: str | None = None
    case_prefix: str | None = None
    rounds: list[ClarificationRound]  # all prior rounds, in order


@router.post("/clarify/followup/stream")
async def clarify_followup_stream(
    req: FollowupRequest,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
):
    """
    Run a follow-up clarification round given the document(s) and prior Q/A history.
    """
    if req.document_id is None and req.mindmap_document_id is None:
        raise HTTPException(status_code=400, detail="至少需要提供 PRD 文档或测试脑图其中之一。")
    if not await _verify_session(req.session_id, project_id):
        raise HTTPException(status_code=404, detail="Session not found")

    async with AsyncSessionLocal() as db:
        prd_doc: Document | None = None
        mindmap_doc: Document | None = None
        if req.document_id is not None:
            r = await db.execute(
                select(Document).where(
                    Document.id == req.document_id, Document.project_id == project_id
                )
            )
            prd_doc = r.scalar_one_or_none()
            if prd_doc is None:
                raise HTTPException(status_code=404, detail="Document not found")
        if req.mindmap_document_id is not None:
            r = await db.execute(
                select(Document).where(
                    Document.id == req.mindmap_document_id, Document.project_id == project_id
                )
            )
            mindmap_doc = r.scalar_one_or_none()
            if mindmap_doc is None:
                raise HTTPException(status_code=404, detail="Mindmap document not found")

    doc_content = truncate_for_llm(prd_doc.raw_text or "") if prd_doc else ""
    mindmap_content = (
        truncate_for_llm(mindmap_doc.raw_text or "") if mindmap_doc else None
    )
    completed_rounds = len(req.rounds)
    next_round_index = completed_rounds + 1
    rounds_dump = [r.model_dump() for r in req.rounds]

    async def events():
        session_id = req.session_id

        async def emit_error(message: str):
            await _upsert_clarification_state(session_id, project_id, status="error")
            msg = await _write_assistant_message(
                session_id, f"⚠️ 追问失败：{message}", kind="followup_error",
                ref={"round": next_round_index},
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("error", {"message": message})

        # Hard cap: skip the LLM call once we've used up our budget.
        if completed_rounds >= MAX_CLARIFICATION_ROUNDS:
            yield _sse("stage", {
                "stage": "max_rounds",
                "message": f"已完成 {MAX_CLARIFICATION_ROUNDS} 轮澄清，达到上限，直接进入生成。",
            })
            clarification = {
                "summary": "已达到最大澄清轮次",
                "module_detected": req.module_name or "未知模块",
                "case_prefix_suggestion": req.case_prefix or "CASE",
                "questions": [],
                "ready_to_generate": True,
            }
            # 注意：状态写 "generating" 而非 "done"——"done" 必须等 /api/generate 把
            # 用例落库后才由生成路由写。否则前端关屏中断 generate 请求后，重开看到
            # status="done" + 用例为空，会卡死。
            await _upsert_clarification_state(
                session_id, project_id,
                rounds=rounds_dump,
                current_questions=[],
                current_round=next_round_index,
                ready_to_generate=True,
                status="generating",
                confirmed_module_name=req.module_name or None,
                confirmed_case_prefix=req.case_prefix or None,
            )
            msg = await _write_assistant_message(
                session_id,
                f"已完成 {MAX_CLARIFICATION_ROUNDS} 轮澄清，达到上限，直接进入生成。",
                kind="followup_done",
                ref={"round": next_round_index, "max_rounds": MAX_CLARIFICATION_ROUNDS},
            )
            yield _sse("assistant_message", {"message": msg})
            yield _sse("result", {
                "round": next_round_index,
                "max_rounds": MAX_CLARIFICATION_ROUNDS,
                "clarification": clarification,
            })
            yield _sse("done", {})
            return

        yield _sse("stage", {
            "stage": "clarifying",
            "message": f"基于第 {completed_rounds} 轮回答继续澄清（第 {next_round_index}/{MAX_CLARIFICATION_ROUNDS} 轮）…",
            "round": next_round_index,
            "max_rounds": MAX_CLARIFICATION_ROUNDS,
        })

        clarification: dict | None = None
        try:
            async for kind, payload in stream_followup_clarification(
                doc_content=doc_content,
                module_name=req.module_name,
                case_prefix=req.case_prefix,
                rounds_history=rounds_dump,
                round_index=next_round_index,
                mindmap_content=mindmap_content,
                system_prompt=await resolve_active_prompt(project_id, "clarifier_followup"),
            ):
                if kind == "token":
                    yield _sse("token", {"content": payload})
                elif kind == "result":
                    clarification = payload
        except Exception as exc:
            logger.exception("Follow-up clarifier failed")
            async for ev in emit_error(str(exc)):
                yield ev
            return

        if clarification is None:
            clarification = {
                "summary": "未生成新的澄清结果",
                "module_detected": req.module_name or "未知模块",
                "case_prefix_suggestion": req.case_prefix or "CASE",
                "questions": [],
                "ready_to_generate": True,
            }

        # If we just finished the last allowed round, force a stop regardless of LLM opinion.
        if next_round_index >= MAX_CLARIFICATION_ROUNDS and clarification.get("questions"):
            logger.info(
                "Follow-up hit max rounds with %d remaining questions — forcing ready_to_generate",
                len(clarification.get("questions") or []),
            )
            clarification["ready_to_generate"] = True
            clarification["questions"] = []
            clarification["summary"] = (
                (clarification.get("summary") or "") + f"（已达到 {MAX_CLARIFICATION_ROUNDS} 轮澄清上限，剩余疑点将由生成器尽力推断。）"
            )

        new_qs = clarification.get("questions") or []
        ready = bool(clarification.get("ready_to_generate"))

        # ready=True 表示该进入生成；用 "generating" 标记，等 /api/generate 完成才写 "done"
        # 同步把 confirmed_module_name / confirmed_case_prefix 落库——前端断网/锁屏后回查时
        # 需要这两字段来恢复"继续生成"按钮入参。
        upsert_kwargs: dict = dict(
            rounds=rounds_dump,
            current_questions=new_qs,
            current_round=next_round_index,
            ready_to_generate=ready,
            status="generating" if ready else "awaiting_answers",
        )
        if ready:
            if req.module_name:
                upsert_kwargs["confirmed_module_name"] = req.module_name
            if req.case_prefix:
                upsert_kwargs["confirmed_case_prefix"] = req.case_prefix
        await _upsert_clarification_state(session_id, project_id, **upsert_kwargs)

        if ready or not new_qs:
            content = f"第 {next_round_index} 轮澄清后已无遗留疑点，可以开始生成测试用例。"
            kind_label = "followup_done"
        else:
            content = f"根据您第 {completed_rounds} 轮回答，又发现 **{len(new_qs)}** 个澄清问题（第 {next_round_index}/{MAX_CLARIFICATION_ROUNDS} 轮）。"
            kind_label = "followup_continue"
        msg = await _write_assistant_message(
            session_id, content, kind=kind_label,
            ref={"round": next_round_index, "questions": len(new_qs)},
        )
        yield _sse("assistant_message", {"message": msg})

        yield _sse("result", {
            "round": next_round_index,
            "max_rounds": MAX_CLARIFICATION_ROUNDS,
            "clarification": clarification,
        })
        yield _sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
