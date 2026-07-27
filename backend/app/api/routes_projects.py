"""Project CRUD.

- 列表：管理员可见全部项目；普通账号可见自己创建的 + 公开项目。
- 创建：管理员不限量；普通账号最多创建一个项目。
- 修改：管理员可改任意项目；普通账号只能改自己创建的项目（含公开/私有切换）。
- 删除：管理员可删任意项目；普通账号只能删除自己创建的项目。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, is_admin
from app.database import get_db
from app.models.user import Project, User

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    is_public: bool = False


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    is_public: bool | None = None


def _project_payload(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "creator_id": p.creator_id,
        "is_public": p.is_public,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/projects")
async def list_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员可见全部项目；普通账号可见自己创建的 + 公开项目。"""
    stmt = select(Project).order_by(Project.created_at.desc())
    if not is_admin(user):
        stmt = stmt.where(or_(Project.creator_id == user.id, Project.is_public.is_(True)))
    result = await db.execute(stmt)
    return [_project_payload(p) for p in result.scalars().all()]


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    req: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = req.name.strip()
    # 普通账号最多只能拥有一个项目
    if not is_admin(user):
        owned = await db.execute(select(Project.id).where(Project.creator_id == user.id))
        if owned.first():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="每个账号只能创建一个项目")
    result = await db.execute(select(Project).where(Project.name == name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="项目名称已存在")
    project = Project(
        name=name,
        description=req.description,
        creator_id=user.id,
        is_public=req.is_public,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _project_payload(project)


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: int,
    req: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员可改任意项目；普通账号只能改自己创建的项目。"""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    if not is_admin(user) and project.creator_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权修改该项目")

    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="项目名称不能为空")
        dup = await db.execute(
            select(Project.id).where(Project.name == name, Project.id != project_id)
        )
        if dup.first():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="项目名称已存在")
        project.name = name
    if req.description is not None:
        project.description = req.description
    if req.is_public is not None:
        project.is_public = req.is_public

    await db.commit()
    await db.refresh(project)
    return _project_payload(project)


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    if not is_admin(user) and project.creator_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权删除该项目")
    await db.delete(project)
    await db.commit()
    return {"status": "deleted", "id": project_id}
