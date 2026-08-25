"""LLM token 用量记账 + 每日配额查询。

**唯一记账入口**：所有 agent 都经 `agents/llm_factory` 构建模型，那里统一挂上本模块的
`UsageRecorder` 回调，所以新增 agent 不需要额外接线，token 自动被算进去。

归属靠 ContextVar：请求入口（见 app/limits.py 的依赖）把当前用户写进 `_owner`，
asyncio 的 Context 随 Task 继承，因此请求里 `create_task` 出去的后台任务（如反馈
diff 分析）也会记到同一个人头上。没有归属的调用（启动后的 prompt 建议巡检等）
直接跳过不记账——它们不该消耗任何用户的配额。

一切失败都吞掉只打日志：记账绝不能拖垮业务调用。
"""
from __future__ import annotations

import datetime as _dt
import logging
from contextvars import ContextVar
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.usage import DailyUsage

logger = logging.getLogger("caseweave.usage")

_owner: ContextVar[int | None] = ContextVar("caseweave_usage_owner", default=None)


# ── 归属上下文 ────────────────────────────────────────────────────────────────

def set_usage_owner(user_id: int | None) -> None:
    """把后续 LLM 调用的 token 记到这个用户头上（作用域 = 当前 asyncio Task 及其子 Task）。"""
    _owner.set(user_id)


def current_usage_owner() -> int | None:
    return _owner.get()


def usage_day(now: _dt.datetime | None = None) -> _dt.date:
    """配额口径下的「今天」。默认按 UTC+8 算，即北京时间 00:00 翻篇。"""
    offset = get_settings().quota_reset_utc_offset_hours
    base = now or _dt.datetime.now(_dt.timezone.utc)
    return (base + _dt.timedelta(hours=offset)).date()


# ── token 提取 ────────────────────────────────────────────────────────────────

def extract_tokens(response: LLMResult) -> tuple[int, int]:
    """从 LLMResult 里取 (input_tokens, output_tokens)，取不到返回 (0, 0)。

    优先走 LangChain 标准化的 `usage_metadata`（Anthropic / OpenAI 都填），
    取不到再退回各家原始的 `llm_output.token_usage`。
    """
    for gens in response.generations or []:
        for gen in gens:
            meta = getattr(getattr(gen, "message", None), "usage_metadata", None)
            if meta:
                return int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)

    raw = response.llm_output or {}
    usage = raw.get("token_usage") or raw.get("usage") or {}
    if isinstance(usage, dict):
        inp = usage.get("prompt_tokens", usage.get("input_tokens")) or 0
        out = usage.get("completion_tokens", usage.get("output_tokens")) or 0
        return int(inp), int(out)
    return 0, 0


# ── 落库 ──────────────────────────────────────────────────────────────────────

async def record_usage(user_id: int, input_tokens: int, output_tokens: int) -> None:
    """把一次调用的 token 累加进 daily_usage（按 user+day upsert）。"""
    async with AsyncSessionLocal() as db:
        table = DailyUsage.__table__
        stmt = (
            pg_insert(table)
            .values(
                user_id=user_id,
                day=usage_day(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                calls=1,
            )
            .on_conflict_do_update(
                index_elements=[table.c.user_id, table.c.day],
                set_={
                    "input_tokens": table.c.input_tokens + input_tokens,
                    "output_tokens": table.c.output_tokens + output_tokens,
                    "calls": table.c.calls + 1,
                    "updated_at": func.now(),
                },
            )
        )
        await db.execute(stmt)
        await db.commit()


async def get_today_tokens(db: AsyncSession, user_id: int) -> int:
    """该用户当天已用 token（input + output）。没有记录返回 0。"""
    row = (
        await db.execute(
            select(DailyUsage.input_tokens, DailyUsage.output_tokens).where(
                DailyUsage.user_id == user_id, DailyUsage.day == usage_day()
            )
        )
    ).first()
    if not row:
        return 0
    return int(row[0] or 0) + int(row[1] or 0)


class UsageRecorder(AsyncCallbackHandler):
    """LangChain 回调：每次 LLM 调用结束就把 token 累加到 daily_usage。"""

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:  # noqa: D102
        user_id = _owner.get()
        inp, out = extract_tokens(response)
        if user_id is None:
            # 无归属（后台巡检等）：不记账，但留个 debug 方便排查「用量对不上」
            logger.debug("LLM 调用无归属，跳过记账 | in=%d out=%d", inp, out)
            return
        if inp <= 0 and out <= 0:
            # provider 没回 usage（如 OpenAI 兼容网关流式且未开 stream_options）
            logger.debug("LLM 调用未返回 usage，跳过记账 | user=%s", user_id)
            return
        try:
            await record_usage(user_id, inp, out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("token 记账失败 user=%s: %s", user_id, exc)
