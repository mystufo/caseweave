"""并发闸门与配额的可观测接口。

前端用 /limits/status 决定要不要提示「今日额度快用完了」；管理员用 /limits/usage
看钱花在谁身上——这是把成本从「月底看账单」变成「随时能查」的那一环。
"""
import datetime as _dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, cast, func, select
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


MAX_BUCKETS = 31  # 前端一屏最多展示的分组数（列数），也是这里的硬上限


def _bucket_starts(granularity: str, periods: int, today: _dt.date) -> list[_dt.date]:
    """生成从近到远的分组起始日（含当前这一格）。

    - day   自然日
    - week  周一为界
    - month 月初为界
    """
    if granularity == "week":
        cur = today - _dt.timedelta(days=today.weekday())
        return [cur - _dt.timedelta(weeks=i) for i in range(periods)]
    if granularity == "month":
        out: list[_dt.date] = []
        y, m = today.year, today.month
        for _ in range(periods):
            out.append(_dt.date(y, m, 1))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        return out
    return [today - _dt.timedelta(days=i) for i in range(periods)]


@router.get("/limits/usage")
async def usage_report(
    granularity: str = Query(default="day", pattern="^(day|week|month)$"),
    periods: int = Query(default=MAX_BUCKETS, ge=1, le=MAX_BUCKETS),
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """管理员：按天/周/月分组的 token 用量，最多回 31 组。

    四个口径一次给全，前端不用来回拉：
      - `buckets`        分组起始日（从近到远），没有用量的分组也占一格
      - `by_user`        区间内按账号汇总（谁花得最多）
      - `by_period`      按分组汇总（趋势）
      - `by_user_period` 账号 × 分组的明细行（前端 pivot 成表格）
    """
    s = get_settings()
    today = usage_day()
    buckets = _bucket_starts(granularity, periods, today)
    since = buckets[-1]

    # 按天时不必绕 date_trunc；按周/月交给 Postgres 分桶，省得把明细全捞回来自己算。
    bucket_col = (
        DailyUsage.day
        if granularity == "day"
        else cast(func.date_trunc(granularity, DailyUsage.day), Date)
    )

    by_user = (await db.execute(
        select(
            DailyUsage.user_id,
            User.email,
            User.name,
            func.sum(DailyUsage.input_tokens).label("input_tokens"),
            func.sum(DailyUsage.output_tokens).label("output_tokens"),
            func.sum(DailyUsage.calls).label("calls"),
        )
        .join(User, User.id == DailyUsage.user_id)
        .where(DailyUsage.day >= since)
        .group_by(DailyUsage.user_id, User.email, User.name)
        .order_by(func.sum(DailyUsage.input_tokens + DailyUsage.output_tokens).desc())
    )).all()

    by_period = (await db.execute(
        select(
            bucket_col.label("period"),
            func.sum(DailyUsage.input_tokens).label("input_tokens"),
            func.sum(DailyUsage.output_tokens).label("output_tokens"),
            func.sum(DailyUsage.calls).label("calls"),
        )
        .where(DailyUsage.day >= since)
        .group_by(bucket_col)
        .order_by(bucket_col.desc())
    )).all()

    by_user_period = (await db.execute(
        select(
            DailyUsage.user_id,
            bucket_col.label("period"),
            func.sum(DailyUsage.input_tokens).label("input_tokens"),
            func.sum(DailyUsage.output_tokens).label("output_tokens"),
            func.sum(DailyUsage.calls).label("calls"),
        )
        .where(DailyUsage.day >= since)
        .group_by(DailyUsage.user_id, bucket_col)
        .order_by(bucket_col.desc(), DailyUsage.user_id)
    )).all()

    admin_set = s.admin_emails_set

    def _user_row(r) -> dict:
        email = r.email or ""
        is_admin_user = email.lower() in admin_set
        return {
            "user_id": r.user_id,
            "email": email,
            "name": r.name,
            "is_admin": is_admin_user,
            # 配额豁免：不限额，或管理员且开了豁免
            "quota_exempt": s.daily_token_quota <= 0 or (s.quota_exempt_admins and is_admin_user),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int((r.input_tokens or 0) + (r.output_tokens or 0)),
            "calls": int(r.calls or 0),
        }

    def _period_row(r) -> dict:
        return {
            "period": r.period.isoformat(),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int((r.input_tokens or 0) + (r.output_tokens or 0)),
            "calls": int(r.calls or 0),
        }

    users = [_user_row(r) for r in by_user]

    return {
        "granularity": granularity,
        "periods": periods,
        "since": since.isoformat(),
        "until": today.isoformat(),
        "buckets": [b.isoformat() for b in buckets],
        "quota": {
            "daily_token_quota": s.daily_token_quota,
            "exempt_admins": s.quota_exempt_admins,
            "reset_utc_offset_hours": s.quota_reset_utc_offset_hours,
        },
        "totals": {
            "input_tokens": sum(u["input_tokens"] for u in users),
            "output_tokens": sum(u["output_tokens"] for u in users),
            "total_tokens": sum(u["total_tokens"] for u in users),
            "calls": sum(u["calls"] for u in users),
            "users": len(users),
        },
        "by_user": users,
        "by_period": [_period_row(r) for r in by_period],
        "by_user_period": [{"user_id": r.user_id, **_period_row(r)} for r in by_user_period],
        "gate": llm_gate.stats(),
    }
