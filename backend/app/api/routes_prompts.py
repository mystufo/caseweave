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
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_project
from app.database import get_db
from app.models.knowledge import PromptVersion
from app.models.user import User
from app.prompts.registry import PROMPT_SPECS, get_spec

logger = logging.getLogger("testcraft.routes_prompts")

router = APIRouter()


class PromptVersionCreate(BaseModel):
    template: str = Field(min_length=1)
    activate: bool = True  # 保存后是否立即设为生效版本


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
    logger.info(
        "Prompt 版本已保存 | key=%s project=%s version=%s active=%s by=%s",
        key, project_id, version.version, req.activate, user.id,
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
