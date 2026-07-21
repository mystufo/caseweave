"""Knowledge base and module management API."""
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.skill_generator import generate_skill_for_module
from app.agents.clarifier import _sanitize_case_prefix
from app.auth import get_current_user, require_project
from app.database import get_db
from app.knowledge.store import search_relevant, store_entries, KnowledgeDraft
from app.config import get_settings
from app.models.clarification import ClarificationState
from app.models.feedback import Feedback, TestCase, FeedbackConsumption
from app.models.knowledge import Document, Module, ModuleRelation, KnowledgeEntry, Skill
from app.models.session import Session
from app.models.user import User
from app.tools.doc_parser import truncate_for_llm

logger = logging.getLogger("testcraft.routes_knowledge")

router = APIRouter()


# ── Modules ───────────────────────────────────────────────────────────────────

class ModuleCreate(BaseModel):
    name: str
    code: str | None = None          # 英文名 = 用例编号前缀（大写 A-Z/0-9/-）
    description: str | None = None
    parent_id: int | None = None


def _normalize_module_code(raw: str | None) -> str | None:
    """把用户/LLM 给的 code 归一为合法编号前缀；空或非法（_sanitize 兜底成 CASE）→ None。"""
    if not (raw or "").strip():
        return None
    cleaned = _sanitize_case_prefix(raw)
    return cleaned if cleaned != "CASE" else None


@router.get("/modules")
async def list_modules(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Module).where(Module.project_id == project_id).order_by(Module.name)
    )
    modules = result.scalars().all()
    return [
        {"id": m.id, "name": m.name, "code": m.code, "description": m.description, "parent_id": m.parent_id}
        for m in modules
    ]


@router.post("/modules")
async def create_module(
    data: ModuleCreate,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    module = Module(
        project_id=project_id,
        name=data.name,
        code=_normalize_module_code(data.code),
        description=data.description,
        parent_id=data.parent_id,
    )
    db.add(module)
    await db.commit()
    await db.refresh(module)
    return {"id": module.id, "name": module.name, "code": module.code}


class ModuleUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    description: str | None = None
    parent_id: int | None = None


@router.put("/modules/{module_id}")
async def update_module(
    module_id: int,
    data: ModuleUpdate,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    module = await _load_project_module(module_id, project_id, db)
    if data.name is not None:
        module.name = data.name
    if data.code is not None:
        module.code = _normalize_module_code(data.code)
    if data.description is not None:
        module.description = data.description
    if data.parent_id is not None:
        module.parent_id = data.parent_id or None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="模块名称已存在")
    await db.refresh(module)
    return {
        "id": module.id,
        "name": module.name,
        "code": module.code,
        "description": module.description,
        "parent_id": module.parent_id,
    }


@router.delete("/modules/{module_id}")
async def delete_module(
    module_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 关联的文档 / Skill / 知识条目 module_id 均为 ON DELETE SET NULL，
    # 删除模块后它们自动变为「未分类」，不会级联丢数据。
    module = await _load_project_module(module_id, project_id, db)
    await db.delete(module)
    await db.commit()
    return {"ok": True}


# ── Module relations ──────────────────────────────────────────────────────────
# 模块关联关系：A "depends_on" / "triggers" / "shares_data" B。
# Generator 在生成用例前会自动按当前模块拉相关 relations 拼到 prompt 里，
# 让 LLM 在跨模块联动场景中考虑上下游影响（例如下单成功 → 库存扣减）。

VALID_RELATION_TYPES = {"depends_on", "triggers", "shares_data", "blocks", "extends"}


class ModuleRelationCreate(BaseModel):
    source_module_id: int
    target_module_id: int
    relation_type: str   # depends_on / triggers / shares_data / blocks / extends
    description: str | None = None


def _relation_payload(r: ModuleRelation, *, source_name: str | None = None,
                      target_name: str | None = None) -> dict:
    return {
        "id": r.id,
        "source_module_id": r.source_module_id,
        "target_module_id": r.target_module_id,
        "source_module_name": source_name,
        "target_module_name": target_name,
        "relation_type": r.relation_type,
        "description": r.description,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def _load_project_module(module_id: int, project_id: int, db: AsyncSession) -> Module:
    """校验模块归属本项目；不存在或跨项目都 404。"""
    r = await db.execute(
        select(Module).where(Module.id == module_id, Module.project_id == project_id)
    )
    module = r.scalar_one_or_none()
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    return module


@router.get("/module_relations")
async def list_module_relations(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    module_id: int | None = Query(default=None, description="只列与该模块相关的关系（source 或 target 命中）"),
):
    """List module relations within the current project.

    Relation 不直接绑定 project_id，但 source/target Module 都隶属 project，
    用 IN (本项目所有 module id) 双向过滤即可隔离跨项目越权读取。
    一次拉模块名映射避免 N+1。"""
    rows = (await db.execute(
        select(ModuleRelation).where(
            ModuleRelation.source_module_id.in_(
                select(Module.id).where(Module.project_id == project_id)
            ),
            ModuleRelation.target_module_id.in_(
                select(Module.id).where(Module.project_id == project_id)
            ),
        ).order_by(ModuleRelation.id.desc())
    )).scalars().all()
    if module_id is not None:
        rows = [r for r in rows
                if r.source_module_id == module_id or r.target_module_id == module_id]
    # 拉一次模块名映射避免 N+1
    name_map: dict[int, str] = {}
    if rows:
        ids = {r.source_module_id for r in rows} | {r.target_module_id for r in rows}
        mods = (await db.execute(
            select(Module).where(Module.id.in_(ids), Module.project_id == project_id)
        )).scalars().all()
        name_map = {m.id: m.name for m in mods}
    return [
        _relation_payload(r,
                          source_name=name_map.get(r.source_module_id),
                          target_name=name_map.get(r.target_module_id))
        for r in rows
    ]


@router.post("/module_relations")
async def create_module_relation(
    data: ModuleRelationCreate,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if data.source_module_id == data.target_module_id:
        raise HTTPException(status_code=400, detail="source 与 target 不能是同一个模块")
    rel_type = (data.relation_type or "").strip()
    if rel_type not in VALID_RELATION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"relation_type 必须是 {sorted(VALID_RELATION_TYPES)} 之一",
        )
    src = await _load_project_module(data.source_module_id, project_id, db)
    tgt = await _load_project_module(data.target_module_id, project_id, db)

    # 防重复：同一对 (src, tgt, type) 已存在则直接返回旧记录（幂等）
    existing = (await db.execute(
        select(ModuleRelation).where(
            ModuleRelation.source_module_id == src.id,
            ModuleRelation.target_module_id == tgt.id,
            ModuleRelation.relation_type == rel_type,
        )
    )).scalar_one_or_none()
    if existing is not None:
        # 描述允许更新（用户可能想补充描述）
        if data.description and existing.description != data.description:
            existing.description = data.description
            await db.commit()
            await db.refresh(existing)
        return _relation_payload(existing, source_name=src.name, target_name=tgt.name)

    rel = ModuleRelation(
        source_module_id=src.id,
        target_module_id=tgt.id,
        relation_type=rel_type,
        description=data.description,
    )
    db.add(rel)
    await db.commit()
    await db.refresh(rel)
    return _relation_payload(rel, source_name=src.name, target_name=tgt.name)


@router.delete("/module_relations/{relation_id}")
async def delete_module_relation(
    relation_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rel = (await db.execute(
        select(ModuleRelation).where(ModuleRelation.id == relation_id)
    )).scalar_one_or_none()
    if rel is None:
        raise HTTPException(status_code=404, detail="Module relation not found")
    # 项目归属校验：只要 source 或 target 不属于本项目就 404（避免跨项目越权删除）
    src = (await db.execute(
        select(Module).where(Module.id == rel.source_module_id, Module.project_id == project_id)
    )).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="Module relation not found")
    await db.delete(rel)
    await db.commit()
    return {"deleted": True, "id": relation_id}


# ── Knowledge entries ─────────────────────────────────────────────────────────

def _entry_payload(e: KnowledgeEntry, *, distance: float | None = None) -> dict:
    return {
        "id": e.id,
        "module_id": e.module_id,
        "document_id": e.document_id,
        "knowledge_type": e.knowledge_type,
        "content": e.content,
        "source": e.source,
        "confidence": e.confidence,
        "version": e.version,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "distance": distance,
    }


@router.get("/knowledge/preview")
async def preview_knowledge_for_session(
    session_id: int = Query(..., description="目标会话 id；从其 ClarificationState 取 document"),
    top_k: int | None = Query(default=None, ge=1, le=50,
        description="覆盖默认条数；不传 → 用 settings.knowledge_preview_top_k"),
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生成前给前端预览：基于本会话已上传文档的 raw_text 在项目知识库里跑一次语义检索，
    返回会被注入 Generator prompt 的候选条目。前端可让用户勾选要用的子集。

    没有 ClarificationState / document_id / 文档 → 返回空数组（让前端走"无知识注入"路径）。
    """
    if top_k is None:
        top_k = get_settings().knowledge_preview_top_k
    # 校验 session 归属本项目
    sr = await db.execute(
        select(Session).where(Session.id == session_id, Session.project_id == project_id)
    )
    if sr.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # 取澄清运行态里的 document_id（生成时也是用它）
    state_q = await db.execute(
        select(ClarificationState).where(ClarificationState.session_id == session_id)
    )
    state = state_q.scalar_one_or_none()
    if state is None or state.document_id is None:
        return {"document_id": None, "module_id": None, "hits": []}

    doc_q = await db.execute(
        select(Document).where(Document.id == state.document_id, Document.project_id == project_id)
    )
    doc = doc_q.scalar_one_or_none()
    if doc is None:
        return {"document_id": None, "module_id": None, "hits": []}

    # query 与生成时保持一致：raw_text 截断到 2000 字
    query = truncate_for_llm(doc.raw_text or "", limit=2000)
    hits = await search_relevant(
        db, project_id=project_id, module_id=doc.module_id, query=query, top_k=top_k,
    )
    if not hits:
        return {"document_id": doc.id, "module_id": doc.module_id, "hits": []}

    # 重新拉详细字段（store 只返回精简版）
    hit_ids = [h.id for h in hits]
    rows = (await db.execute(
        select(KnowledgeEntry).where(KnowledgeEntry.id.in_(hit_ids))
    )).scalars().all()
    by_id = {e.id: e for e in rows}
    dist_by_id = {h.id: h.distance for h in hits}
    payload = [
        _entry_payload(by_id[h.id], distance=dist_by_id.get(h.id))
        for h in hits
        # 排除"由这份文档自身抽取出"的条目，避免自反馈污染（同文档刚被异步抽取写入的 entry
        # 不该再当外部上下文喂回 Clarifier/Generator）
        if h.id in by_id and by_id[h.id].document_id != doc.id
    ]
    return {"document_id": doc.id, "module_id": doc.module_id, "hits": payload}


@router.get("/knowledge/stats")
async def knowledge_stats(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    recent_days: int = Query(default=7, ge=1, le=30),
):
    """项目知识库总览数据：总条目、按类型分布、按模块分布、近 N 天新增。

    给前端 KnowledgePage header 当 dashboard：让用户一眼看到知识库规模、有没有
    覆盖盲区（某模块条目极少）、近期是否还有"输入"。Phase 4 反馈循环也要靠这个底座
    判断知识库是否在持续生长。
    """
    # 总条目数
    total = (await db.execute(
        select(func.count(KnowledgeEntry.id)).where(KnowledgeEntry.project_id == project_id)
    )).scalar_one() or 0

    # 按类型聚合
    type_rows = (await db.execute(
        select(KnowledgeEntry.knowledge_type, func.count(KnowledgeEntry.id))
        .where(KnowledgeEntry.project_id == project_id)
        .group_by(KnowledgeEntry.knowledge_type)
    )).all()
    by_type = [{"knowledge_type": t or "unknown", "count": int(c)} for t, c in type_rows]
    by_type.sort(key=lambda x: -x["count"])

    # 按模块聚合（含 NULL=项目级），同时拉模块名映射
    module_rows = (await db.execute(
        select(KnowledgeEntry.module_id, func.count(KnowledgeEntry.id))
        .where(KnowledgeEntry.project_id == project_id)
        .group_by(KnowledgeEntry.module_id)
    )).all()
    mod_ids = [m for m, _ in module_rows if m is not None]
    name_map: dict[int, str] = {}
    if mod_ids:
        mods = (await db.execute(
            select(Module.id, Module.name).where(
                Module.id.in_(mod_ids), Module.project_id == project_id,
            )
        )).all()
        name_map = {mid: mname for mid, mname in mods}
    by_module = []
    for mid, cnt in module_rows:
        by_module.append({
            "module_id": mid,
            "module_name": name_map.get(mid) if mid is not None else None,
            "count": int(cnt),
        })
    by_module.sort(key=lambda x: -x["count"])

    # 最近 N 天新增数
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    recent_added = (await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.created_at >= cutoff,
        )
    )).scalar_one() or 0

    # 文档级数据：本项目已上传 / 还有 pending_knowledge 草稿没审核的份数
    doc_total = (await db.execute(
        select(func.count(Document.id)).where(Document.project_id == project_id)
    )).scalar_one() or 0
    pending_docs = (await db.execute(
        select(func.count(Document.id)).where(
            Document.project_id == project_id,
            Document.pending_knowledge.isnot(None),
        )
    )).scalar_one() or 0

    # 模块覆盖：项目下有几个 module 至少有一条 knowledge
    project_module_count = (await db.execute(
        select(func.count(Module.id)).where(Module.project_id == project_id)
    )).scalar_one() or 0
    covered_modules = sum(1 for m in by_module if m["module_id"] is not None and m["count"] > 0)

    return {
        "total": int(total),
        "recent_added": int(recent_added),
        "recent_days": recent_days,
        "by_type": by_type,
        "by_module": by_module,
        "documents": {
            "total": int(doc_total),
            "with_pending_drafts": int(pending_docs),
        },
        "module_coverage": {
            "modules_total": int(project_module_count),
            "modules_with_knowledge": covered_modules,
        },
    }


@router.get("/knowledge")
async def list_or_search_knowledge(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, description="自然语言查询；命中走 pgvector 余弦近邻"),
    module_id: int | None = Query(default=None, description="可选模块过滤；为空表示项目全量"),
    only_orphan: bool = Query(default=False, description="为 true 时仅返回 module_id IS NULL 的项目级条目（搭配 module_id 不传使用）"),
    top_k: int = Query(default=20, ge=1, le=500),
):
    """List or semantically search knowledge entries within the current project.

    - `q` 给出 → 走 search_relevant（embedding 不可用时自动退化为按 confidence/时间排序）
    - 不给 q → 按 confidence DESC、时间倒序返回（可选 module 过滤）
    """
    if q and q.strip():
        hits = await search_relevant(
            db, project_id=project_id, module_id=module_id, query=q.strip(), top_k=top_k,
        )
        if not hits:
            return []
        # 把命中条目重新拉一次拿全字段（store 只返回精简版）
        hit_ids = [h.id for h in hits]
        rows = (await db.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.id.in_(hit_ids))
        )).scalars().all()
        by_id = {e.id: e for e in rows}
        dist_by_id = {h.id: h.distance for h in hits}
        result = [_entry_payload(by_id[h.id], distance=dist_by_id.get(h.id))
                  for h in hits if h.id in by_id]
        # 搜索路径下 only_orphan 在 store.search_relevant 不支持，这里做后过滤
        if only_orphan:
            result = [r for r in result if r["module_id"] is None]
        return result

    stmt = (
        select(KnowledgeEntry)
        .where(KnowledgeEntry.project_id == project_id)
        .order_by(KnowledgeEntry.confidence.desc(), KnowledgeEntry.created_at.desc())
        .limit(top_k)
    )
    if only_orphan:
        stmt = stmt.where(KnowledgeEntry.module_id.is_(None))
    elif module_id is not None:
        stmt = stmt.where(KnowledgeEntry.module_id == module_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [_entry_payload(e) for e in rows]


@router.get("/knowledge/{module_id}")
async def get_knowledge(
    module_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify the module belongs to this project.
    result = await db.execute(
        select(Module).where(Module.id == module_id, Module.project_id == project_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Module not found")

    result = await db.execute(
        select(KnowledgeEntry)
        .where(KnowledgeEntry.module_id == module_id, KnowledgeEntry.project_id == project_id)
        .order_by(KnowledgeEntry.confidence.desc())
    )
    entries = result.scalars().all()
    return [_entry_payload(e) for e in entries]


class KnowledgeUpdate(BaseModel):
    content: str | None = None
    confidence: float | None = None
    # 是否改所属模块：apply_module=true 时按 module_id 更新（None=转为项目级）。
    # 不传时不动 module_id（保持既有行为）。
    apply_module: bool = False
    module_id: int | None = None


async def _load_entry_scoped(entry_id: int, project_id: int, db: AsyncSession) -> KnowledgeEntry:
    """按 project 校验加载条目；条目可能 module_id=NULL（项目级），也可能挂在某 module 上。
    两种情况都要直接验 KnowledgeEntry.project_id，避免 module_id=NULL 时被 INNER JOIN 过滤掉。"""
    result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.id == entry_id,
            KnowledgeEntry.project_id == project_id,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    return entry


@router.put("/knowledge/{entry_id}")
async def update_knowledge(
    entry_id: int,
    data: KnowledgeUpdate,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entry = await _load_entry_scoped(entry_id, project_id, db)

    if data.content is not None:
        entry.content = data.content
        entry.version += 1
    if data.confidence is not None:
        entry.confidence = data.confidence
    if data.apply_module:
        # 校验目标模块归属（None=转项目级，跳过校验）
        if data.module_id is not None:
            mr = await db.execute(
                select(Module).where(
                    Module.id == data.module_id, Module.project_id == project_id
                )
            )
            if mr.scalar_one_or_none() is None:
                raise HTTPException(status_code=404, detail="Module not found")
        entry.module_id = data.module_id

    await db.commit()
    return {"id": entry.id, "version": entry.version, "module_id": entry.module_id}


@router.delete("/knowledge/{entry_id}")
async def delete_knowledge(
    entry_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除单条知识条目；幂等返回 {deleted: True, id}（不存在则 404）。"""
    entry = await _load_entry_scoped(entry_id, project_id, db)
    await db.delete(entry)
    await db.commit()
    return {"deleted": True, "id": entry_id}


# ── Documents（需求文档 / 脑图，按模块聚合查看） ─────────────────────────────

def _document_summary(doc: Document) -> dict:
    return {
        "id": doc.id,
        "filename": doc.filename,
        "file_type": doc.file_type,
        "source_type": doc.source_type,
        "source_url": doc.source_url,
        "role": doc.role,
        "module_id": doc.module_id,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
    }


@router.get("/documents")
async def list_documents(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    module_id: int | None = Query(default=None, description="可选模块过滤；为空表示项目全量"),
    only_orphan: bool = Query(default=False, description="为 true 时仅返回 module_id IS NULL 的未分类文档"),
    role: str | None = Query(default=None, description="可选 role 过滤：prd / mindmap"),
):
    stmt = (
        select(Document)
        .where(Document.project_id == project_id)
        .order_by(Document.uploaded_at.desc())
    )
    if only_orphan:
        stmt = stmt.where(Document.module_id.is_(None))
    elif module_id is not None:
        stmt = stmt.where(Document.module_id == module_id)
    if role:
        stmt = stmt.where(Document.role == role)
    rows = (await db.execute(stmt)).scalars().all()
    return [_document_summary(d) for d in rows]


async def _load_project_document(document_id: int, project_id: int, db: AsyncSession) -> Document:
    r = await db.execute(
        select(Document).where(Document.id == document_id, Document.project_id == project_id)
    )
    doc = r.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/documents/{document_id}")
async def get_document(
    document_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """文档详情，含截断后的解析正文（只读预览用）。

    额外回传原始正文总字符数与是否被截断，供前端提示"当次文档共 N 字 / 已截断"。
    """
    doc = await _load_project_document(document_id, project_id, db)
    payload = _document_summary(doc)
    raw = doc.raw_text or ""
    content = truncate_for_llm(raw) if raw else ""
    payload["content"] = content
    payload["raw_text_length"] = len(raw)
    payload["truncated"] = len(raw) > len(content)
    return payload


class DocumentModuleUpdate(BaseModel):
    module_id: int | None = None   # null = 取消归属（未分类）


@router.patch("/documents/{document_id}")
async def update_document_module(
    document_id: int,
    data: DocumentModuleUpdate,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """改文档归属模块。module_id 为 null 表示取消归属。"""
    doc = await _load_project_document(document_id, project_id, db)
    if data.module_id is not None:
        # 校验目标模块归属本项目（不存在 / 跨项目都 404）
        await _load_project_module(data.module_id, project_id, db)
    doc.module_id = data.module_id
    await db.commit()
    await db.refresh(doc)
    return _document_summary(doc)


# ── Pending knowledge drafts (人工确认入库闸门) ──────────────────────────────

@router.get("/documents/{document_id}/pending_knowledge")
async def get_pending_knowledge(
    document_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """读取某文档当前的草稿列表（用户审核界面用）。

    返回:
      {"document_id": int, "drafts": list[{knowledge_type, content, source, confidence}],
       "settled": bool}
    settled=True 表示 pending_knowledge 已为空（None 或 []）—— 用户已审核完或本就没抽到。
    """
    r = await db.execute(
        select(Document).where(Document.id == document_id, Document.project_id == project_id)
    )
    doc = r.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    pending = doc.pending_knowledge
    return {
        "document_id": doc.id,
        "drafts": pending if isinstance(pending, list) else [],
        "settled": pending is None,
    }


class AcceptedDraftIn(BaseModel):
    # 用户在前端编辑后回传的完整草稿。允许修改 content / knowledge_type / confidence。
    knowledge_type: str
    content: str
    source: str | None = "document"
    confidence: float | None = 0.6
    # 用户选择"保留新的（替换旧条目）"时，这里带上被替代的已有条目 id 列表；
    # 入库该草稿的同时会硬删除这些旧条目。空/None = 不替换任何旧条目。
    supersedes_entry_ids: list[int] | None = None


class ConfirmPendingKnowledgeRequest(BaseModel):
    # 两种入参形式（任选其一；都给以 accepted_drafts 为准）：
    #   - accepted_drafts: 用户编辑后的完整草稿列表（推荐——支持改 content / type）
    #   - accepted_indices: 仅勾选不编辑时按 GET 顺序的索引
    # 都不传 / null → 全部按原 pending 入库；都给空列表 → 一条都不入。
    accepted_indices: list[int] | None = None
    accepted_drafts: list[AcceptedDraftIn] | None = None
    # 用户在草稿审核面板里选择"加入哪个模块"：
    #   - apply_module=false（默认）→ 沿用文档当前 module_id（保持既有行为）
    #   - apply_module=true 且 module_id 为某模块 → 按该模块入库，并把该模块回写到文档
    #   - apply_module=true 且 module_id=None → 显式"不归入模块"（项目级，module_id=NULL）
    apply_module: bool = False
    module_id: int | None = None


@router.post("/documents/{document_id}/confirm_pending_knowledge")
async def confirm_pending_knowledge(
    document_id: int,
    body: ConfirmPendingKnowledgeRequest,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把用户勾选的草稿真正写入 knowledge_entries，未选的直接丢弃；
    无论入多少条，操作完成都把 pending_knowledge 清空（设为 None）。"""
    r = await db.execute(
        select(Document).where(Document.id == document_id, Document.project_id == project_id)
    )
    doc = r.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    pending = doc.pending_knowledge
    if not isinstance(pending, list):
        # 已 settle 过；幂等成功
        return {"document_id": doc.id, "stored": 0, "settled": True}

    # 解析入库目标模块：用户在审核面板显式选了模块 → 以它为准（含"不归入模块"=None），
    # 并回写到文档；否则沿用文档当前 module_id（保持既有行为）。
    target_module_id = doc.module_id
    if body.apply_module:
        if body.module_id is not None:
            mr = await db.execute(
                select(Module).where(
                    Module.id == body.module_id, Module.project_id == project_id
                )
            )
            if mr.scalar_one_or_none() is None:
                raise HTTPException(status_code=404, detail="Module not found")
        target_module_id = body.module_id
        doc.module_id = target_module_id

    # 优先使用前端编辑过的 accepted_drafts；否则按 indices 从原 pending 取。
    # supersedes_ids 收集所有草稿选择"替换"掉的旧条目 id（仅 accepted_drafts 路径可带）。
    supersedes_ids: set[int] = set()
    if body.accepted_drafts is not None:
        selected_raw: list[dict] = [
            {
                "knowledge_type": d.knowledge_type,
                "content": d.content,
                "source": d.source or "document",
                "confidence": d.confidence if d.confidence is not None else 0.6,
            }
            for d in body.accepted_drafts
        ]
        for d in body.accepted_drafts:
            for eid in (d.supersedes_entry_ids or []):
                if isinstance(eid, int):
                    supersedes_ids.add(eid)
    elif body.accepted_indices is None:
        selected_raw = list(pending)
    else:
        valid = sorted({i for i in body.accepted_indices if isinstance(i, int) and 0 <= i < len(pending)})
        selected_raw = [pending[i] for i in valid]

    drafts: list[KnowledgeDraft] = []
    for raw in selected_raw:
        if not isinstance(raw, dict):
            continue
        kt = str(raw.get("knowledge_type") or "").strip()
        content = str(raw.get("content") or "").strip()
        if not kt or not content:
            continue
        try:
            conf = float(raw.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        drafts.append(KnowledgeDraft(
            knowledge_type=kt, content=content,
            source=str(raw.get("source") or "document"),
            confidence=max(0.1, min(0.95, conf)),
        ))

    # 先删被替代的旧条目（"保留新的"）：逐个校验归属本项目后硬删，与写入同一事务提交。
    superseded = 0
    for eid in supersedes_ids:
        entry = await _load_entry_scoped(eid, project_id, db)
        await db.delete(entry)
        superseded += 1

    stored = 0
    if drafts:
        stored = await store_entries(
            db, project_id=project_id, module_id=target_module_id,
            document_id=doc.id, drafts=drafts,
        )

    # 清空 pending，标记已审核
    doc.pending_knowledge = None
    await db.commit()
    return {
        "document_id": doc.id, "stored": stored, "superseded": superseded,
        "settled": True, "module_id": target_module_id,
    }


# ── Skills ────────────────────────────────────────────────────────────────────

@router.get("/skills")
async def list_skills(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Skills live under modules; filter via Module.project_id.
    result = await db.execute(
        select(Skill)
        .join(Module, Module.id == Skill.module_id, isouter=True)
        .where((Module.project_id == project_id) | (Skill.module_id.is_(None)))
        .order_by(Skill.name)
    )
    skills = result.scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "module_id": s.module_id,
            "source": s.source,
            "version": s.version,
        }
        for s in skills
    ]


@router.get("/skills/{skill_id}")
async def get_skill(
    skill_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.module_id is not None:
        # 通过 module 反查 project 验证归属
        m = await db.execute(
            select(Module).where(Module.id == skill.module_id, Module.project_id == project_id)
        )
        if m.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Skill not found")
    return {
        "id": skill.id,
        "name": skill.name,
        "module_id": skill.module_id,
        "content": skill.content,
        "source": skill.source,
        "version": skill.version,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


# ── Skills CRUD（Phase 4 新增） ───────────────────────────────────────────────

class SkillCreate(BaseModel):
    name: str
    content: str
    module_id: int | None = None


class SkillUpdate(BaseModel):
    name: str | None = None
    content: str | None = None
    module_id: int | None = None


async def _ensure_module_belongs(
    db: AsyncSession, *, module_id: int | None, project_id: int,
) -> None:
    if module_id is None:
        return
    r = await db.execute(
        select(Module.id).where(Module.id == module_id, Module.project_id == project_id)
    )
    if r.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Module not found")


async def _verify_skill_in_project(
    db: AsyncSession, *, skill_id: int, project_id: int,
) -> Skill:
    r = await db.execute(select(Skill).where(Skill.id == skill_id))
    skill = r.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.module_id is not None:
        m = await db.execute(
            select(Module).where(Module.id == skill.module_id, Module.project_id == project_id)
        )
        if m.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.post("/skills")
async def create_skill(
    body: SkillCreate,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = (body.name or "").strip()
    content = (body.content or "").strip()
    if not name or not content:
        raise HTTPException(status_code=400, detail="name 和 content 必填")
    await _ensure_module_belongs(db, module_id=body.module_id, project_id=project_id)
    skill = Skill(
        name=name,
        module_id=body.module_id,
        content=content,
        source="manual",
        version=1,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return {
        "id": skill.id, "name": skill.name, "module_id": skill.module_id,
        "content": skill.content, "source": skill.source, "version": skill.version,
    }


@router.put("/skills/{skill_id}")
async def update_skill(
    skill_id: int,
    body: SkillUpdate,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    skill = await _verify_skill_in_project(db, skill_id=skill_id, project_id=project_id)
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name 不能为空")
        skill.name = new_name
    if body.content is not None:
        new_content = body.content.strip()
        if not new_content:
            raise HTTPException(status_code=400, detail="content 不能为空")
        skill.content = new_content
        # 用户改了内容相当于发新版
        skill.version = (skill.version or 1) + 1
    if body.module_id is not None:
        await _ensure_module_belongs(db, module_id=body.module_id, project_id=project_id)
        skill.module_id = body.module_id
    await db.commit()
    await db.refresh(skill)
    return {
        "id": skill.id, "name": skill.name, "module_id": skill.module_id,
        "content": skill.content, "source": skill.source, "version": skill.version,
    }


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    skill = await _verify_skill_in_project(db, skill_id=skill_id, project_id=project_id)
    await db.delete(skill)
    await db.commit()
    return {"id": skill_id, "deleted": True}


class SkillRegenerateRequest(BaseModel):
    module_id: int
    feedback_limit: int = 10
    knowledge_limit: int = 10


@router.post("/skills/regenerate")
async def regenerate_skill(
    body: SkillRegenerateRequest,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """根据该模块最近的 edit 反馈分析 + 用户反馈衍生的知识条目，LLM 归纳一份 Skill。

    - 命中既有 auto_module_{id} 行 → 替换 content + version+1
    - 否则新建一行
    - 信号不足 / LLM 失败 → 返回 {"created": False, "reason": "..."}（HTTP 200，不扔异常）
    """
    # 校验 module 归属并取名字
    mr = await db.execute(
        select(Module).where(Module.id == body.module_id, Module.project_id == project_id)
    )
    module = mr.scalar_one_or_none()
    if module is None:
        raise HTTPException(status_code=404, detail="Module not found")

    # 拉该模块待消费的 skill 反馈：分诊到 skill、且尚未被 skill 出口消费（问题 B）
    consumed_skill_subq = (
        select(FeedbackConsumption.feedback_id)
        .where(FeedbackConsumption.output_kind == "skill")
    ).scalar_subquery()

    fb_rows = (await db.execute(
        select(Feedback)
        .join(TestCase, TestCase.id == Feedback.test_case_id)
        .where(
            TestCase.project_id == project_id,
            TestCase.module == module.name,
            Feedback.diff_analysis.is_not(None),
            Feedback.triage_targets.isnot(None),
            Feedback.triage_targets.like("%skill%"),
            Feedback.id.notin_(consumed_skill_subq),
        )
        .order_by(Feedback.id.desc())
        .limit(max(1, min(body.feedback_limit, 50)))
    )).scalars().all()

    feedback_samples: list[dict] = []
    consumed_feedback_ids: list[int] = []
    for fb in fb_rows:
        try:
            obj = json.loads(fb.diff_analysis) if fb.diff_analysis else {}
        except (TypeError, ValueError):
            obj = {}
        if not isinstance(obj, dict):
            continue
        feedback_samples.append({
            "intent": obj.get("intent"),
            "summary": obj.get("summary"),
        })
        consumed_feedback_ids.append(fb.id)

    # 拉该模块下 source='user_feedback' 的最新若干条知识
    ke_rows = (await db.execute(
        select(KnowledgeEntry.content)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.module_id == body.module_id,
            KnowledgeEntry.source == "user_feedback",
        )
        .order_by(KnowledgeEntry.id.desc())
        .limit(max(1, min(body.knowledge_limit, 50)))
    )).scalars().all()
    knowledge_entries = [c for c in ke_rows if c]

    auto_name = f"auto_module_{body.module_id}"
    existing = (await db.execute(
        select(Skill).where(Skill.module_id == body.module_id, Skill.name == auto_name)
    )).scalar_one_or_none()

    # 短路：没有任何待消费的新反馈、且已存在自动 Skill —— 无新信号，跳过重复归纳（省一次 LLM）
    if not consumed_feedback_ids and existing is not None:
        logger.info(
            "skill regenerate skipped | module=%s no new feedback (existing skill_id=%d)",
            module.name, existing.id,
        )
        return {
            "created": False,
            "reason": "无新反馈，跳过重复归纳",
            "feedback_count": 0,
            "knowledge_count": len(knowledge_entries),
        }

    # 有旧 Skill 时进入增量合并模式：在旧备忘单基础上并入新信号，而非整篇覆盖
    skill_md = await generate_skill_for_module(
        module_name=module.name,
        feedback_samples=feedback_samples,
        knowledge_entries=knowledge_entries,
        existing_skill=existing.content if existing is not None else None,
    )
    if not skill_md:
        return {
            "created": False,
            "reason": "样本不足或 LLM 未产出有效经验",
            "feedback_count": len(feedback_samples),
            "knowledge_count": len(knowledge_entries),
        }

    if existing is not None:
        existing.content = skill_md
        existing.version = (existing.version or 1) + 1
        existing.source = "auto_generated"
        skill = existing
        action = "updated"
    else:
        skill = Skill(
            name=auto_name,
            module_id=body.module_id,
            content=skill_md,
            source="auto_generated",
            version=1,
        )
        db.add(skill)
        action = "created"

    await db.commit()
    await db.refresh(skill)

    # 消费回写：把本次用到的反馈记为"已被 skill 出口消费"（幂等），避免下次重复归纳同一批
    if consumed_feedback_ids:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        await db.execute(
            pg_insert(FeedbackConsumption)
            .values([
                {"feedback_id": fid, "output_kind": "skill", "output_ref_id": skill.id}
                for fid in consumed_feedback_ids
            ])
            .on_conflict_do_nothing(constraint="uq_feedback_consumption")
        )
        await db.commit()

    logger.info(
        "skill regenerated | module=%s skill_id=%d action=%s feedback=%d knowledge=%d",
        module.name, skill.id, action, len(feedback_samples), len(knowledge_entries),
    )
    return {
        "created": True,
        "action": action,
        "skill": {
            "id": skill.id,
            "name": skill.name,
            "module_id": skill.module_id,
            "content": skill.content,
            "source": skill.source,
            "version": skill.version,
        },
        "feedback_count": len(feedback_samples),
        "knowledge_count": len(knowledge_entries),
    }
