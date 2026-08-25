"""并发闸门与配额的可观测接口。

前端用 /limits/status 决定要不要提示「今日额度快用完了」；管理员用 /limits/usage
看钱花在谁身上——这是把成本从「月底看账单」变成「随时能查」的那一环。
"""
import datetime as _dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, is_admin, require_admin
from app.config import get_settings
from app.database import get_db
from app.limits import llm_gate
from app.models.usage import DailyUsage
from app.models.user import User
from app.usage import get_today_tokens, usage_day

router = APIRouter()


@router.get("/limits/status")
async def limits_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户的配额余量 + 全局闸门实时状态。"""
    s = get_settings()
    used = await get_today_tokens(db, user.id)
    quota = s.daily_token_quota
    exempt = quota <= 0 or (s.quota_exempt_admins and is_admin(user))
    return {
        "quota": {
            "daily_token_quota": quota,
            "used_tokens": used,
            "remaining_tokens": None if exempt else max(0, quota - used),
            "exempt": exempt,
            "day": usage_day().isoformat(),
            "reset_utc_offset_hours": s.quota_reset_utc_offset_hours,
        },
        "gate": llm_gate.stats(),
    }


@router.get("/limits/usage")
async def usage_report(
    days: int = Query(default=7, ge=1, le=90),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：近 N 天的 token 用量，按用户聚合 + 按天汇总。"""
    since = usage_day() - _dt.timedelta(days=days - 1)

    by_user = (await db.execute(
        select(
            DailyUsage.user_id,
            User.email,
            func.sum(DailyUsage.input_tokens).label("input_tokens"),
            func.sum(DailyUsage.output_tokens).label("output_tokens"),
            func.sum(DailyUsage.calls).label("calls"),
        )
        .join(User, User.id == DailyUsage.user_id)
        .where(DailyUsage.day >= since)
        .group_by(DailyUsage.user_id, User.email)
        .order_by(func.sum(DailyUsage.input_tokens + DailyUsage.output_tokens).desc())
    )).all()

    by_day = (await db.execute(
        select(
            DailyUsage.day,
            func.sum(DailyUsage.input_tokens).label("input_tokens"),
            func.sum(DailyUsage.output_tokens).label("output_tokens"),
            func.sum(DailyUsage.calls).label("calls"),
        )
        .where(DailyUsage.day >= since)
        .group_by(DailyUsage.day)
        .order_by(DailyUsage.day.desc())
    )).all()

    return {
        "since": since.isoformat(),
        "days": days,
        "by_user": [
            {
                "user_id": r.user_id,
                "email": r.email,
                "input_tokens": int(r.input_tokens or 0),
                "output_tokens": int(r.output_tokens or 0),
                "total_tokens": int((r.input_tokens or 0) + (r.output_tokens or 0)),
                "calls": int(r.calls or 0),
            }
            for r in by_user
        ],
        "by_day": [
            {
                "day": r.day.isoformat(),
                "input_tokens": int(r.input_tokens or 0),
                "output_tokens": int(r.output_tokens or 0),
                "total_tokens": int((r.input_tokens or 0) + (r.output_tokens or 0)),
                "calls": int(r.calls or 0),
            }
            for r in by_day
        ],
        "gate": llm_gate.stats(),
    }
