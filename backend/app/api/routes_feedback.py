"""Feedback API: collect likes/dislikes and edit diffs for test cases.

Phase 4：edit 反馈触发后台 LLM 分析（diff_analyzer），把意图 + 抽出的产品规则
分别落到 feedbacks.diff_analysis 和 knowledge_entries(source='user_feedback')。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.diff_analyzer import (
    DiffAnalysis,
    analyze_edit,
    has_real_diff,
)
from app.agents.feedback_triage import triage_intent, classify_dislike, targets_to_str
from app.auth import get_current_user, require_project
from app.database import AsyncSessionLocal, get_db
from app.knowledge.store import store_entries
from app.models.feedback import TestCase, Feedback, FeedbackConsumption
from app.models.knowledge import Module
from app.models.user import User

logger = logging.getLogger("testcraft.routes_feedback")

router = APIRouter()


class FeedbackRequest(BaseModel):
    test_case_id: int
    feedback_type: str  # like / dislike / edit
    original_content: dict | None = None
    modified_content: dict | None = None
    reason: str | None = None  # dislike 可选原因文本（进化链路 3）


async def _record_consumption(
    db: AsyncSession, feedback_id: int, output_kind: str, output_ref_id: int | None
) -> None:
    """写一条消费台账（幂等：撞唯一约束就忽略）。调用方负责 commit。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(FeedbackConsumption)
        .values(feedback_id=feedback_id, output_kind=output_kind, output_ref_id=output_ref_id)
        .on_conflict_do_nothing(constraint="uq_feedback_consumption")
    )
    await db.execute(stmt)


async def _analyze_diff_bg(
    *,
    feedback_id: int,
    project_id: int,
    module_name: str | None,
    original: dict[str, Any] | None,
    modified: dict[str, Any] | None,
) -> None:
    """异步后台任务：跑 diff 分析，回写 diff_analysis + triage/targets；规则入知识库并记消费台账。

    完全 fail-open：任何异常都吞掉 + log warning，不影响主请求已经返回的结果。
    """
    try:
        analysis: DiffAnalysis | None = await analyze_edit(
            original=original, modified=modified, module_name=module_name,
        )
        if analysis is None:
            logger.info("diff analyzer returned None for feedback=%d (no changes or LLM fail)", feedback_id)
            return

        targets = triage_intent(analysis.intent)

        async with AsyncSessionLocal() as db:
            # 回写 diff_analysis（JSON-as-text）+ 分诊结果
            r = await db.execute(select(Feedback).where(Feedback.id == feedback_id))
            fb = r.scalar_one_or_none()
            if fb is None:
                logger.warning("feedback %d gone before diff analysis landed", feedback_id)
                return
            fb.diff_analysis = json.dumps(analysis.to_jsonable(), ensure_ascii=False)
            fb.triage = analysis.intent
            fb.triage_targets = targets_to_str(targets)

            # 入知识库（仅当分诊到 knowledge 且 LLM 真的抽到了规则）
            if "knowledge" in targets and analysis.extracted_rules:
                # 反查 module_id：用例上的 module 字段是名字，按 (project, name) 找；找不到走 project-level NULL
                module_id: int | None = None
                if module_name:
                    mq = await db.execute(
                        select(Module.id).where(
                            Module.project_id == project_id,
                            Module.name == module_name,
                        )
                    )
                    module_id = mq.scalar_one_or_none()
                inserted = await store_entries(
                    db,
                    project_id=project_id,
                    module_id=module_id,
                    document_id=None,
                    drafts=analysis.extracted_rules,
                )
                # 知识出口在"分析即消费"（store_entries 返回计数不返回 id，ref 记 None）
                if inserted > 0:
                    await _record_consumption(db, feedback_id, "knowledge", None)
                logger.info(
                    "diff analyzer | feedback=%d module=%s intent=%s targets=%s rules_inserted=%d",
                    feedback_id, module_name, analysis.intent, targets, inserted,
                )
            else:
                logger.info(
                    "diff analyzer | feedback=%d intent=%s targets=%s (no knowledge consumption)",
                    feedback_id, analysis.intent, targets,
                )

            await db.commit()
    except Exception as exc:
        logger.warning("diff analysis background task failed (feedback=%d): %s", feedback_id, exc)


async def _analyze_dislike_bg(
    *,
    feedback_id: int,
    module_name: str | None,
    reason: str,
    case: dict[str, Any] | None,
) -> None:
    """带原因的 dislike：LLM 归一到 intent → 写 triage/targets（让 dislike 也进分诊链路）。

    不直接产知识（dislike 无 before/after diff，抽不出规则）；只打分诊标签，
    供 prompt/skill 出口按 target 取用。fail-open。
    """
    try:
        intent = await classify_dislike(reason=reason, case=case, module_name=module_name)
        targets = triage_intent(intent)
        async with AsyncSessionLocal() as db:
            fb = (await db.execute(select(Feedback).where(Feedback.id == feedback_id))).scalar_one_or_none()
            if fb is None:
                return
            # 存一份精简 analysis，让下游 prompt/skill 聚合能读到 intent/summary
            fb.diff_analysis = json.dumps(
                {"intent": intent, "summary": reason.strip()[:200], "changed_fields": []},
                ensure_ascii=False,
            )
            fb.triage = intent
            fb.triage_targets = targets_to_str(targets)
            await db.commit()
        logger.info("dislike triaged | feedback=%d intent=%s targets=%s", feedback_id, intent, targets)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dislike analysis background task failed (feedback=%d): %s", feedback_id, exc)


@router.post("/feedback")
async def submit_feedback(
    request: FeedbackRequest,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TestCase).where(
            TestCase.id == request.test_case_id,
            TestCase.project_id == project_id,
        )
    )
    test_case = result.scalar_one_or_none()
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")

    feedback = Feedback(
        test_case_id=request.test_case_id,
        feedback_type=request.feedback_type,
        original_content=request.original_content,
        modified_content=request.modified_content,
        reason=(request.reason or None),
    )
    db.add(feedback)

    if request.feedback_type == "edit" and request.modified_content:
        mc = request.modified_content
        for field in ("name", "preconditions", "steps", "expected_result", "remarks"):
            if field in mc:
                setattr(test_case, field, mc[field])
        # priority 走白名单（防止前端误传任意字符串）
        if "priority" in mc and mc["priority"] in ("P1", "P2", "P3"):
            test_case.priority = mc["priority"]

    await db.commit()
    await db.refresh(feedback)

    # 仅 edit 类型 + 实际有非空字段变化时才触发 diff 分析（避免空 edit 浪费 LLM 调用）
    if (
        feedback.feedback_type == "edit"
        and has_real_diff(request.original_content, request.modified_content)
    ):
        asyncio.create_task(_analyze_diff_bg(
            feedback_id=feedback.id,
            project_id=project_id,
            module_name=test_case.module,
            original=request.original_content,
            modified=request.modified_content,
        ))
    # dislike 带原因：走归一分诊链路，让👎也能进化（问题 A）
    elif feedback.feedback_type == "dislike" and (request.reason or "").strip():
        case_snapshot = {
            "name": test_case.name,
            "steps": test_case.steps,
            "expected_result": test_case.expected_result,
        }
        asyncio.create_task(_analyze_dislike_bg(
            feedback_id=feedback.id,
            module_name=test_case.module,
            reason=request.reason or "",
            case=case_snapshot,
        ))

    return {"id": feedback.id, "status": "recorded"}


@router.get("/feedback/recent")
async def list_recent_feedback(
    module_id: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """最近 N 条已分析的 edit 反馈（diff_analysis IS NOT NULL）。

    供 KnowledgePage "最近修改沉淀" 卡使用——前端按 module 过滤。
    """
    stmt = (
        select(Feedback, TestCase)
        .join(TestCase, TestCase.id == Feedback.test_case_id)
        .where(
            TestCase.project_id == project_id,
            Feedback.feedback_type == "edit",
            Feedback.diff_analysis.is_not(None),
        )
        .order_by(Feedback.id.desc())
        .limit(limit)
    )

    # module_id → 反查 module name 后按字符串过滤（test_cases.module 存的是名字）
    if module_id is not None:
        mq = await db.execute(
            select(Module.name).where(Module.id == module_id, Module.project_id == project_id)
        )
        mod_name = mq.scalar_one_or_none()
        if mod_name is None:
            return {"items": []}
        stmt = stmt.where(TestCase.module == mod_name)

    rows = (await db.execute(stmt)).all()
    items: list[dict[str, Any]] = []
    for fb, tc in rows:
        analysis_obj: dict[str, Any] = {}
        try:
            analysis_obj = json.loads(fb.diff_analysis) if fb.diff_analysis else {}
        except (TypeError, ValueError):
            analysis_obj = {}
        rules = analysis_obj.get("extracted_rules") or []
        items.append({
            "id": fb.id,
            "test_case_id": tc.id,
            "test_case_name": tc.name,
            "module": tc.module,
            "intent": analysis_obj.get("intent"),
            "summary": analysis_obj.get("summary"),
            "changed_fields": analysis_obj.get("changed_fields") or [],
            "extracted_rule_count": len(rules) if isinstance(rules, list) else 0,
            "created_at": fb.created_at.isoformat() if fb.created_at else None,
        })
    return {"items": items}


@router.get("/feedback/negative")
async def list_negative_feedback(
    module_id: int | None = Query(default=None),
    feedback_type: str | None = Query(default=None, description="edit / dislike；不传=两者都要"),
    limit: int = Query(default=50, ge=1, le=200),
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """负反馈单独视图数据源：已分析的 edit + 带原因 dislike 反馈（diff_analysis IS NOT NULL）。

    每条返回本次归纳的完整信息——意图/摘要/改动字段/原因/分诊出口/**归纳出的规则全文**，
    以及该反馈已被哪些进化出口消费（knowledge/skill/prompt）。供「负反馈」页面把
    每次归纳的相关记录独立展示，不再和文档来源的知识混在一起。
    """
    stmt = (
        select(Feedback, TestCase)
        .join(TestCase, TestCase.id == Feedback.test_case_id)
        .where(
            TestCase.project_id == project_id,
            Feedback.feedback_type.in_(("edit", "dislike")),
            Feedback.diff_analysis.is_not(None),
        )
        .order_by(Feedback.id.desc())
        .limit(limit)
    )
    if feedback_type in ("edit", "dislike"):
        stmt = stmt.where(Feedback.feedback_type == feedback_type)

    # module_id → 反查 module name 后按字符串过滤（test_cases.module 存的是名字）
    if module_id is not None:
        mq = await db.execute(
            select(Module.name).where(Module.id == module_id, Module.project_id == project_id)
        )
        mod_name = mq.scalar_one_or_none()
        if mod_name is None:
            return {"items": []}
        stmt = stmt.where(TestCase.module == mod_name)

    rows = (await db.execute(stmt)).all()
    if not rows:
        return {"items": []}

    # 一次性拉这批反馈的消费台账：feedback_id → set(output_kind)
    fb_ids = [fb.id for fb, _ in rows]
    consumed = (await db.execute(
        select(FeedbackConsumption.feedback_id, FeedbackConsumption.output_kind)
        .where(FeedbackConsumption.feedback_id.in_(fb_ids))
    )).all()
    consumed_map: dict[int, list[str]] = {}
    for fid, kind in consumed:
        consumed_map.setdefault(fid, []).append(kind)

    items: list[dict[str, Any]] = []
    for fb, tc in rows:
        analysis_obj: dict[str, Any] = {}
        try:
            analysis_obj = json.loads(fb.diff_analysis) if fb.diff_analysis else {}
        except (TypeError, ValueError):
            analysis_obj = {}
        raw_rules = analysis_obj.get("extracted_rules")
        rules = [
            {
                "knowledge_type": r.get("knowledge_type"),
                "content": r.get("content"),
                "confidence": r.get("confidence"),
            }
            for r in raw_rules
            if isinstance(r, dict) and (r.get("content") or "").strip()
        ] if isinstance(raw_rules, list) else []
        items.append({
            "id": fb.id,
            "feedback_type": fb.feedback_type,
            "test_case_id": tc.id,
            "test_case_name": tc.name,
            "module": tc.module,
            "intent": analysis_obj.get("intent") or fb.triage,
            "summary": analysis_obj.get("summary"),
            "changed_fields": analysis_obj.get("changed_fields") or [],
            "reason": fb.reason,
            "triage_targets": [t for t in (fb.triage_targets.split(",") if fb.triage_targets else []) if t],
            "extracted_rules": rules,
            "consumed_by": consumed_map.get(fb.id, []),
            "created_at": fb.created_at.isoformat() if fb.created_at else None,
        })
    return {"items": items}


@router.get("/feedback/evolution/summary")
async def evolution_summary(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """反馈进化总览：三出口（knowledge/skill/prompt）的待消费/已消费计数 + 分诊分布。

    第二步「反馈进化」面板的数据源；第一步先落 API，可 curl 验证。
    """
    # 本项目所有已分诊反馈（triage_targets 非空）
    triaged = (await db.execute(
        select(Feedback.id, Feedback.triage, Feedback.triage_targets)
        .join(TestCase, TestCase.id == Feedback.test_case_id)
        .where(TestCase.project_id == project_id, Feedback.triage_targets.isnot(None))
    )).all()

    # 已消费映射：feedback_id → set(kinds)
    consumed = (await db.execute(
        select(FeedbackConsumption.feedback_id, FeedbackConsumption.output_kind)
        .join(Feedback, Feedback.id == FeedbackConsumption.feedback_id)
        .join(TestCase, TestCase.id == Feedback.test_case_id)
        .where(TestCase.project_id == project_id)
    )).all()
    consumed_map: dict[int, set[str]] = {}
    for fid, kind in consumed:
        consumed_map.setdefault(fid, set()).add(kind)

    outputs = {k: {"pending": 0, "consumed": 0} for k in ("knowledge", "skill", "prompt")}
    intent_dist: dict[str, int] = {}
    for fid, intent, targets in triaged:
        if intent:
            intent_dist[intent] = intent_dist.get(intent, 0) + 1
        for kind in (targets.split(",") if targets else []):
            if kind not in outputs:
                continue
            if kind in consumed_map.get(fid, set()):
                outputs[kind]["consumed"] += 1
            else:
                outputs[kind]["pending"] += 1

    return {
        "outputs": outputs,               # 每出口 待消费/已消费
        "intent_distribution": intent_dist,  # 归一 intent 分布
        "triaged_total": len(triaged),
    }
