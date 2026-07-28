"""Prompt 版本化管理 API（Phase 4.2 第一阶段）。

按项目隔离地管理 3 个 system prompt（clarifier_initial / clarifier_followup /
generator）的版本：
  - 列出所有可管理的 prompt 及其当前生效版本概览
  - 列出某个 prompt 的全部历史版本
  - 取某个 prompt 的“原始建议版本”（代码默认常量）作为编辑基础
  - 基于编辑结果保存为新版本
  - 选择激活哪个版本（切换 is_active）

权限：登录用户都可读写（与知识库 / Skill 一致），按 X-Project-Id 隔离。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.sql import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_project
from app.database import get_db
from app.models.feedback import Feedback, TestCase, FeedbackConsumption
from app.models.knowledge import PromptSuggestion, PromptVersion
from app.models.user import User
from app.prompts.registry import PROMPT_SPECS, get_spec, get_active_prompt_text
from app.agents.prompt_optimizer import suggest_generator_prompt

logger = logging.getLogger("caseweave.routes_prompts")

router = APIRouter()


class PromptVersionCreate(BaseModel):
    template: str = Field(min_length=1)
    activate: bool = True  # 保存后是否立即设为生效版本
    from_suggestion_id: int | None = None  # 若本次保存源自某条系统建议，标该建议为 adopted


GENERATOR_KEY = "generator"  # 本期唯一支持「改进建议」的 prompt


def _suggestion_payload(s: PromptSuggestion) -> dict:
    return {
        "id": s.id,
        "prompt_id": s.prompt_id,
        "base_version_id": s.base_version_id,
        "base_template": s.base_template,
        "suggested_template": s.suggested_template,
        "rationale": s.rationale,
        "evidence": s.evidence,
        "status": s.status,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


async def _collect_generator_feedback_samples(
    db: AsyncSession, project_id: int, limit: int = 30
) -> list[dict]:
    """聚合该项目 generator 待消费的负反馈：分诊到 prompt 且尚未被 prompt 出口消费。

    只取 triage_targets 含 'prompt' 的反馈，并排除已在 feedback_consumptions(kind=prompt)
    记录过的——保证同一批反馈不被反复分析（问题 B）。每条带回 feedback_id 供消费回写。
    """
    consumed_subq = (
        select(FeedbackConsumption.feedback_id)
        .where(FeedbackConsumption.output_kind == "prompt")
    ).scalar_subquery()

    rows = (await db.execute(
        select(Feedback)
        .join(TestCase, TestCase.id == Feedback.test_case_id)
        .where(
            TestCase.project_id == project_id,
            Feedback.diff_analysis.is_not(None),
            Feedback.triage_targets.isnot(None),
            Feedback.triage_targets.like("%prompt%"),
            Feedback.id.notin_(consumed_subq),
        )
        .order_by(Feedback.id.desc())
        .limit(max(1, min(limit, 100)))
    )).scalars().all()

    samples: list[dict] = []
    for fb in rows:
        try:
            obj = json.loads(fb.diff_analysis) if fb.diff_analysis else {}
        except (TypeError, ValueError):
            obj = {}
        if not isinstance(obj, dict):
            continue
        samples.append({
            "feedback_id": fb.id,
            "intent": obj.get("intent"),
            "summary": obj.get("summary"),
            "changed_fields": obj.get("changed_fields"),
        })
    return samples


async def run_generator_suggestion(
    db: AsyncSession,
    project_id: int,
    *,
    min_samples: int | None = None,
) -> dict:
    """生成并落库一条 generator 的 pending 改进建议（只写草稿，绝不激活）。

    返回 {created:bool, reason?, suggestion?, feedback_count}。
    去重：同一 base_template 已有 pending 建议时不重复插。
    供 API（手动触发）与 lifespan 后台任务共用。
    """
    samples = await _collect_generator_feedback_samples(db, project_id)

    # 取当前生效模板作为基线（无自定义版本时即代码默认常量）
    current = await get_active_prompt_text(db, project_id, GENERATOR_KEY)

    # 定位当前生效版本 id（用于 base_version_id；None=用的是代码默认）
    active_row = (await db.execute(
        select(PromptVersion.id).where(
            PromptVersion.project_id == project_id,
            PromptVersion.prompt_id == GENERATOR_KEY,
            PromptVersion.is_active == 1,
        ).order_by(PromptVersion.id.desc()).limit(1)
    )).scalar_one_or_none()

    result = await suggest_generator_prompt(
        current_template=current,
        feedback_samples=samples,
        min_samples=min_samples,
    )
    outcome = result.get("outcome")
    if outcome != "ok":
        # 把 agent 的 outcome 码映射成用户能读懂的区分性文案
        reason_map = {
            "insufficient_samples": "负反馈样本不足，暂不生成建议",
            "no_signal": "系统判定这批负反馈无需修改生成提示词——多为具体产品规则/口味修正，"
                         "更适合沉淀为知识或 Skill，而非改通用提示词",
            "llm_failed": "LLM 调用失败，请稍后重试",
            "parse_failed": "LLM 返回格式异常，未产出可用建议",
            "contract_violation": "LLM 产出破坏了用例输出契约（缺关键字段/编号规则），已丢弃以保护生成质量",
            "identical": "系统认为当前提示词已足够好，无需修改",
        }
        return {
            "created": False,
            "outcome": outcome,
            "reason": reason_map.get(outcome, "未产出可用改进"),
            "feedback_count": len(samples),
        }

    # 去重：同基线已有 pending 建议就不再重复生成
    dup = (await db.execute(
        select(PromptSuggestion.id).where(
            PromptSuggestion.project_id == project_id,
            PromptSuggestion.prompt_id == GENERATOR_KEY,
            PromptSuggestion.status == "pending",
            PromptSuggestion.base_template == current,
        ).limit(1)
    )).scalar_one_or_none()
    if dup is not None:
        return {
            "created": False,
            "outcome": "duplicate",
            "reason": "已有针对当前版本的待审核建议，请先处理",
            "feedback_count": len(samples),
        }

    suggestion = PromptSuggestion(
        project_id=project_id,
        prompt_id=GENERATOR_KEY,
        base_version_id=active_row,
        base_template=current,
        suggested_template=result["suggested_template"],
        rationale=result.get("rationale"),
        evidence={
            "feedback_count": len(samples),
            "feedback_ids": [s["feedback_id"] for s in samples if s.get("feedback_id")],
            "samples": [{k: v for k, v in s.items() if k != "feedback_id"} for s in samples[:12]],
        },
        status="pending",
    )
    db.add(suggestion)
    await db.commit()
    await db.refresh(suggestion)
    logger.info(
        "prompt suggestion created | project=%s base_version=%s feedback=%d",
        project_id, active_row, len(samples),
    )
    return {
        "created": True,
        "suggestion": _suggestion_payload(suggestion),
        "feedback_count": len(samples),
    }


def _version_payload(v: PromptVersion) -> dict:
    return {
        "id": v.id,
        "prompt_id": v.prompt_id,
        "version": v.version,
        "template": v.template,
        "is_active": bool(v.is_active),
        "created_by": v.created_by,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


@router.get("/prompts")
async def list_prompts(
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出全部可管理的 prompt + 该项目的版本概览（生效版本、版本数、是否在用自定义）。"""
    result = await db.execute(
        select(PromptVersion).where(PromptVersion.project_id == project_id)
    )
    rows = result.scalars().all()
    by_key: dict[str, list[PromptVersion]] = {}
    for r in rows:
        by_key.setdefault(r.prompt_id, []).append(r)

    out = []
    for spec in PROMPT_SPECS:
        versions = by_key.get(spec.key, [])
        active = next((v for v in versions if v.is_active), None)
        out.append({
            "key": spec.key,
            "purpose": spec.purpose,
            "label": spec.label,
            "description": spec.description,
            "version_count": len(versions),
            # 没有自定义版本时，生效的就是代码默认常量
            "using_default": active is None,
            "active_version_id": active.id if active else None,
            "active_version": active.version if active else None,
        })
    return out


@router.get("/prompts/{key}/default")
async def get_prompt_default(
    key: str,
    _project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
):
    """返回某 prompt 的原始建议版本（代码默认常量），供前端载入做编辑基础。"""
    spec = get_spec(key)
    if not spec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未知的 prompt key")
    return {
        "key": spec.key,
        "label": spec.label,
        "description": spec.description,
        "template": spec.default_text,
    }


@router.get("/prompts/{key}/versions")
async def list_prompt_versions(
    key: str,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出某 prompt 在该项目下的全部历史版本（新→旧）。"""
    if not get_spec(key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未知的 prompt key")
    result = await db.execute(
        select(PromptVersion)
        .where(PromptVersion.project_id == project_id, PromptVersion.prompt_id == key)
        .order_by(PromptVersion.id.desc())
    )
    return [_version_payload(v) for v in result.scalars().all()]


@router.post("/prompts/{key}/versions", status_code=status.HTTP_201_CREATED)
async def create_prompt_version(
    key: str,
    req: PromptVersionCreate,
    project_id: int = Depends(require_project),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """基于编辑内容保存为新版本；activate=True 时同时设为生效（取消同组其它生效标记）。"""
    spec = get_spec(key)
    if not spec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未知的 prompt key")

    template = req.template.strip()
    if not template:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="提示词内容不能为空")

    # 版本号 = (project, key) 现有版本数 + 1，保存为字符串序号
    existing = await db.execute(
        select(PromptVersion).where(
            PromptVersion.project_id == project_id, PromptVersion.prompt_id == key
        )
    )
    next_num = len(existing.scalars().all()) + 1

    if req.activate:
        # 先把同组所有版本置为非生效，确保只有一个 is_active
        await db.execute(
            update(PromptVersion)
            .where(PromptVersion.project_id == project_id, PromptVersion.prompt_id == key)
            .values(is_active=0)
        )

    version = PromptVersion(
        project_id=project_id,
        prompt_id=key,
        purpose=spec.purpose,
        version=str(next_num),
        template=template,
        is_active=1 if req.activate else 0,
        created_by=user.id,
    )
    db.add(version)
    await db.commit()
    await db.refresh(version)

    # 若本次保存源自某条系统建议，把它标为 adopted，并把其依据的反馈记为"已被 prompt 消费"
    # （弱关联，失败不影响保存）。dismiss 不走这里 → 反馈不消费，仍可用于下次。
    if req.from_suggestion_id is not None:
        try:
            sug = (await db.execute(
                select(PromptSuggestion).where(
                    PromptSuggestion.id == req.from_suggestion_id,
                    PromptSuggestion.project_id == project_id,
                    PromptSuggestion.prompt_id == key,
                )
            )).scalar_one_or_none()
            if sug is not None and sug.status == "pending":
                sug.status = "adopted"
                sug.reviewed_by = user.id
                sug.reviewed_at = func.now()
                # 消费回写：把建议 evidence 里的 feedback_ids 记入台账（幂等）
                fids = (sug.evidence or {}).get("feedback_ids") or []
                if fids:
                    from sqlalchemy.dialects.postgresql import insert as pg_insert
                    await db.execute(
                        pg_insert(FeedbackConsumption)
                        .values([
                            {"feedback_id": fid, "output_kind": "prompt", "output_ref_id": version.id}
                            for fid in fids
                        ])
                        .on_conflict_do_nothing(constraint="uq_feedback_consumption")
                    )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("标记建议 adopted / 记消费失败 id=%s: %s", req.from_suggestion_id, exc)

    logger.info(
        "Prompt 版本已保存 | key=%s project=%s version=%s active=%s by=%s from_suggestion=%s",
        key, project_id, version.version, req.activate, user.id, req.from_suggestion_id,
    )
    return _version_payload(version)


@router.post("/prompts/{key}/versions/{version_id}/activate")
async def activate_prompt_version(
    key: str,
    version_id: int,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把指定版本设为生效（取消同组其它生效标记）。"""
    if not get_spec(key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未知的 prompt key")
    result = await db.execute(
        select(PromptVersion).where(
            PromptVersion.id == version_id,
            PromptVersion.project_id == project_id,
            PromptVersion.prompt_id == key,
        )
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="版本不存在")

    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.project_id == project_id, PromptVersion.prompt_id == key)
        .values(is_active=0)
    )
    target.is_active = 1
    await db.commit()
    await db.refresh(target)
    logger.info("Prompt 版本已切换生效 | key=%s project=%s version_id=%s", key, project_id, version_id)
    return _version_payload(target)


@router.post("/prompts/{key}/reset")
async def reset_prompt_to_default(
    key: str,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """恢复使用原始建议版本：取消该项目下该 prompt 的所有生效标记。

    历史版本保留（不删），只是回到“用代码默认常量”状态。
    """
    if not get_spec(key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未知的 prompt key")
    await db.execute(
        update(PromptVersion)
        .where(PromptVersion.project_id == project_id, PromptVersion.prompt_id == key)
        .values(is_active=0)
    )
    await db.commit()
    logger.info("Prompt 恢复默认 | key=%s project=%s", key, project_id)
    return {"key": key, "using_default": True}


# ── Phase 4.2 二阶段：系统给的改进建议（只读建议 + 人工审核；本期仅 generator）──────

@router.post("/prompts/{key}/suggestions/generate")
async def generate_prompt_suggestion(
    key: str,
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """分析该项目 generator 的负反馈，产出一条 pending 改进建议（不激活）。

    信号不足 / LLM 未产出 / 已有待审核建议：返回 {created:false, reason}（HTTP 200）。
    """
    if key != GENERATOR_KEY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="本期仅支持 generator 的改进建议")
    from app.config import get_settings
    return await run_generator_suggestion(
        db, project_id, min_samples=get_settings().prompt_suggestion_min_samples,
    )


@router.get("/prompts/{key}/suggestions")
async def list_prompt_suggestions(
    key: str,
    status_filter: str = "pending",
    project_id: int = Depends(require_project),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出该 prompt 在本项目下的建议（默认只看 pending，新→旧）。"""
    if not get_spec(key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未知的 prompt key")
    stmt = (
        select(PromptSuggestion)
        .where(PromptSuggestion.project_id == project_id, PromptSuggestion.prompt_id == key)
        .order_by(PromptSuggestion.id.desc())
    )
    if status_filter and status_filter != "all":
        stmt = stmt.where(PromptSuggestion.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [_suggestion_payload(s) for s in rows]


@router.post("/prompts/suggestions/{suggestion_id}/dismiss")
async def dismiss_prompt_suggestion(
    suggestion_id: int,
    project_id: int = Depends(require_project),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """忽略一条建议（status=dismissed）。"""
    sug = (await db.execute(
        select(PromptSuggestion).where(
            PromptSuggestion.id == suggestion_id,
            PromptSuggestion.project_id == project_id,
        )
    )).scalar_one_or_none()
    if sug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="建议不存在")
    if sug.status == "pending":
        sug.status = "dismissed"
        sug.reviewed_by = user.id
        sug.reviewed_at = func.now()
        await db.commit()
    return {"id": suggestion_id, "status": sug.status}
