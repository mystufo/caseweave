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
    diff_changed_fields,
    has_real_diff,
)
from app.auth import get_current_user, require_project
from app.database import AsyncSessionLocal, get_db
from app.knowledge.store import store_entries
from app.models.feedback import TestCase, Feedback
from app.models.knowledge import Module
from app.models.user import User

logger = logging.getLogger("testcraft.routes_feedback")

router = APIRouter()


class FeedbackRequest(BaseModel):
    test_case_id: int
    feedback_type: str  # like / dislike / edit
    original_content: dict | None = None
    modified_content: dict | None = None


async def _analyze_diff_bg(
    *,
    feedback_id: int,
    project_id: int,
    module_name: str | None,
    original: dict[str, Any] | None,
    modified: dict[str, Any] | None,
) -> None:
    """异步后台任务：跑 diff 分析，把结果回写 feedbacks.diff_analysis；规则入知识库。

    完全 fail-open：任何异常都吞掉 + log warning，不影响主请求已经返回的结果。
    """
    try:
        analysis: DiffAnalysis | None = await analyze_edit(
            original=original, modified=modified, module_name=module_name,
        )
        if analysis is None:
            logger.info("diff analyzer returned None for feedback=%d (no changes or LLM fail)", feedback_id)
            return

        async with AsyncSessionLocal() as db:
            # 回写 diff_analysis（JSON-as-text）
            r = await db.execute(select(Feedback).where(Feedback.id == feedback_id))
            fb = r.scalar_one_or_none()
            if fb is None:
                logger.warning("feedback %d gone before diff analysis landed", feedback_id)
                return
            fb.diff_analysis = json.dumps(analysis.to_jsonable(), ensure_ascii=False)

            # 入知识库（仅当 LLM 真的抽到了规则）
            if analysis.extracted_rules:
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
                logger.info(
                    "diff analyzer | feedback=%d module=%s intent=%s rules_extracted=%d rules_inserted=%d",
                    feedback_id, module_name, analysis.intent,
                    len(analysis.extracted_rules), inserted,
                )
            else:
                logger.info(
                    "diff analyzer | feedback=%d intent=%s no rules extracted",
                    feedback_id, analysis.intent,
                )

            await db.commit()
    except Exception as exc:
        logger.warning("diff analysis background task failed (feedback=%d): %s", feedback_id, exc)


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
