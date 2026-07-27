"""Auth helpers: password hashing, JWT, and FastAPI dependencies."""
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Header, status
from passlib.context import CryptContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.user import Project, User

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def create_access_token(user_id: int, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.jwt_expire_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = int(payload.get("sub", 0))
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def is_admin(user: User) -> bool:
    return (user.email or "").lower() in settings.admin_emails_set


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


async def admin_user_ids(db: AsyncSession) -> list[int]:
    """管理员账号的 user.id 列表（邮箱在白名单里）。无管理员则返回空列表。"""
    if not settings.admin_emails_set:
        return []
    result = await db.execute(
        select(User.id).where(func.lower(User.email).in_(settings.admin_emails_set))
    )
    return list(result.scalars().all())


async def authorize_project(pid: int, user: User, db: AsyncSession) -> Project:
    """校验用户对项目的访问权限。

    可访问：管理员（任意项目）、项目创建者本人、以及公开项目（对所有人可见）。
    """
    result = await db.execute(select(Project).where(Project.id == pid))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    if is_admin(user) or project.creator_id == user.id or project.is_public:
        return project
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该项目")


async def require_project(
    x_project_id: Optional[str] = Header(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> int:
    """Extract project_id from X-Project-Id header (all tenanted routes require it).

    同时做访问控制：普通账号只能访问自己创建的项目，管理员不受限。
    """
    if not x_project_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing X-Project-Id header")
    try:
        pid = int(x_project_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Project-Id")
    if pid <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Project-Id")
    await authorize_project(pid, user, db)
    return pid
