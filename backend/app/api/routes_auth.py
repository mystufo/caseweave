"""Auth API: register, login, profile."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    is_admin,
    verify_password,
)
from app.database import get_db
from app.models.user import User

router = APIRouter()


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    name: str | None = Field(default=None, max_length=80)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name or user.email.split("@")[0],
        "is_admin": is_admin(user),
    }


@router.post("/auth/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    email = req.email.lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=email,
        name=(req.name or "").strip() or None,
        password_hash=hash_password(req.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(user.id, user.email)
    return {"token": token, "user": _user_payload(user)}


@router.post("/auth/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    email = req.email.lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    token = create_access_token(user.id, user.email)
    return {"token": token, "user": _user_payload(user)}


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    return _user_payload(user)
