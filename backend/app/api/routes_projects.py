"""Project CRUD: list (any user), create/delete (admin only)."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models.user import Project, User

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None


def _project_payload(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "creator_id": p.creator_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/projects")
async def list_projects(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """All authenticated users see every project (open-access tenancy)."""
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    return [_project_payload(p) for p in result.scalars().all()]


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    req: ProjectCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    name = req.name.strip()
    result = await db.execute(select(Project).where(Project.name == name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project name already exists")
    project = Project(name=name, description=req.description, creator_id=admin.id)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _project_payload(project)


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    await db.delete(project)
    await db.commit()
    return {"status": "deleted", "id": project_id}
