"""LLM 重接口的并发闸门与每日 token 配额。

三层管控，从外到内：

1. **每账号并发**（`LLM_MAX_CONCURRENCY_PER_USER`，默认 1）——同一账号同时最多跑 N 个
   重任务。超了先等一个很短的宽限窗口（`LLM_PER_USER_GRACE_SECONDS`，默认 3s）吸收时序
   毛刺，还不行就 **429**、不进队列：这层是防「一个人开五个标签页把全局名额占满」，
   直接报错比让他自己排自己更直观。
2. **全局并发**（`LLM_MAX_CONCURRENCY`，默认 3）——整台机器同时最多 M 个重任务在调 LLM。
   超了进 FIFO 队列；队列满（`LLM_QUEUE_SIZE`）或等待超时（`LLM_QUEUE_TIMEOUT_SECONDS`）
   才 429。SSE 接口排队期间持续推 `queued` 帧，前端能显示「前面还有 N 个」。
3. **每日 token 配额**（`DAILY_TOKEN_QUOTA`，默认 0 = 不限）——按 daily_usage 表当天已用
   token 判定，见 app/usage.py。并发只压峰值、不压总量，这层才真正封顶成本。
   判定发生在任务开始前，所以是软封顶：最后一个任务可能小幅超出上限。

为什么是进程内信号量而不是 Redis：后端是单进程 uvicorn（docker-compose 没开 --workers），
进程内状态就是全局状态。将来要开多 worker，把 LLMGate 换成 Redis 实现即可，调用方无需改动。

给路由加闸门：

- **非流式**：`_slot: Ticket = Depends(llm_slot)` —— 依赖自己取号/等待/归还，路由体零改动。
- **流式（SSE）**：`_ticket: Ticket = Depends(llm_ticket)` 只取号不等待，再把原来的事件
  生成器交给 `llm_gate.wrap_stream(_ticket, events)` 塞进 StreamingResponse。排队进度会以
  `queued` 帧推给前端，名额一直握到流跑完。
  ⚠️ 流式路由**不能**靠 yield 依赖的 teardown 归还名额：FastAPI 的 AsyncExitStack 在
  `return response` **之前**就关闭了（见 fastapi/routing.py `get_request_handler`），
  流还没开始跑名额就被还回去了。`wrap_stream` 靠同步置位的 `Ticket.adopted` 接管归还责任。
- **后台任务**：`async with background_slot("label")`，见下方 BACKGROUND_USER_ID。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, is_admin
from app.config import get_settings
from app.database import get_db
from app.models.user import User
from app.usage import get_today_tokens, set_usage_owner

logger = logging.getLogger("caseweave.limits")

_RETRY_AFTER = "20"

# 后台（非用户直接触发）的 LLM 调用统一挂在这个虚拟账号下：既能被全局并发闸门约束、
# 排在真实用户后面，又不会占掉某个真人的「单账号并发」名额。
BACKGROUND_USER_ID = 0


def _sse(event: str, data: dict) -> str:
    """与 routes_upload/_sse、routes_chat 的帧格式保持一致：data: {"type": ..., ...}。"""
    return f"data: {json.dumps({'type': event, **data}, ensure_ascii=False)}\n\n"


class Ticket:
    """一次重任务对名额的持有凭证。由 LLMGate 创建，务必在 finally 里 release。"""

    __slots__ = (
        "user_id", "label", "future", "deadline",
        "granted", "holds_slot", "released", "adopted",
    )

    def __init__(self, user_id: int, label: str) -> None:
        self.user_id = user_id
        self.label = label
        self.future: asyncio.Future | None = None  # None = 没排过队
        self.deadline: float = 0.0
        self.granted = False      # 已经可以开跑
        self.holds_slot = False   # 占着一个全局名额（不限并发时始终 False）
        self.released = False
        # 已被 wrap_stream 接管：归还责任转移给流生成器，依赖的 teardown 不要再动它
        self.adopted = False


class ServiceBusy(HTTPException):
    """闸门拒绝：队列满 / 排队超时 / 单账号并发超限 / 配额用尽，统一 429。"""

    def __init__(self, detail: str, code: str) -> None:
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers={"Retry-After": _RETRY_AFTER, "X-Limit-Code": code},
        )
        self.code = code


class LLMGate:
    """全局并发闸门。单进程、单事件循环，所有状态都是普通对象，无需加锁。"""

    def __init__(
        self,
        *,
        limit: int,
        per_user: int,
        queue_size: int,
        wait_timeout: float,
        per_user_grace: float = 3.0,
    ) -> None:
        self.limit = limit
        self.per_user = per_user
        self.queue_size = queue_size
        self.wait_timeout = wait_timeout
        self.per_user_grace = per_user_grace
        self._running = 0
        self._waiters: deque[Ticket] = deque()
        self._user_active: dict[int, int] = {}

    # ── 状态 ────────────────────────────────────────────────────────────────
    @property
    def running(self) -> int:
        return self._running

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def stats(self) -> dict:
        return {
            "running": self._running,
            "waiting": len(self._waiters),
            "limit": self.limit,
            "per_user_limit": self.per_user,
            "queue_size": self.queue_size,
            "wait_timeout_seconds": self.wait_timeout,
            "per_user_grace_seconds": self.per_user_grace,
        }

    def position(self, ticket: Ticket) -> int:
        """排在第几位（1 起）。已拿到名额或已不在队列里返回 0。"""
        try:
            return self._waiters.index(ticket) + 1
        except ValueError:
            return 0

    # ── 取号 / 等待 / 归还 ──────────────────────────────────────────────────
    async def reserve(self, user_id: int, label: str) -> Ticket:
        """取号：能立刻开跑就直接给名额，否则进全局队列。超限抛 429。

        「单账号并发」超限时不会立刻翻脸，而是先等一个很短的宽限窗口
        （per_user_grace，默认 3s）。这是为了吸收几种正常的时序毛刺：
          - 前端在 SSE 的 done 帧到达时立刻发下一个请求，而服务端刚要归还名额；
          - 用户点了「停止任务」马上重来，上一个连接的清理还没跑完；
          - 双击、重试。
        真·两个标签页各跑一个长任务的情况，等满 3s 照样 429，该拦的还是拦得住。
        """
        deadline = time.monotonic() + self.per_user_grace
        # 后台虚拟账号只受全局并发约束：它天然是排队等待的，卡「单账号并发」只会让
        # 后台活儿互相拒绝，没有意义。
        gated = user_id != BACKGROUND_USER_ID and self.per_user > 0
        while gated and self._user_active.get(user_id, 0) >= self.per_user:
            if time.monotonic() >= deadline:
                raise ServiceBusy(
                    f"你已有 {self._user_active.get(user_id, 0)} 个生成任务在进行中，"
                    "请等它跑完再发起新的。",
                    "per_user_concurrency",
                )
            await asyncio.sleep(0.05)

        # 循环退出到这里之间没有 await，所以名额计数不会被别的协程插队改掉。
        active = self._user_active.get(user_id, 0)
        ticket = Ticket(user_id, label)
        if self.limit <= 0:
            ticket.granted = True  # 不限并发：只做每账号那层
        elif self._running < self.limit and not self._waiters:
            self._running += 1
            ticket.granted = True
            ticket.holds_slot = True
        else:
            if self.queue_size > 0 and len(self._waiters) >= self.queue_size:
                raise ServiceBusy(
                    f"当前排队人数已满（{len(self._waiters)}），请稍后再试。",
                    "queue_full",
                )
            ticket.future = asyncio.get_running_loop().create_future()
            ticket.deadline = time.monotonic() + self.wait_timeout
            self._waiters.append(ticket)

        self._user_active[user_id] = active + 1
        if not ticket.granted:
            logger.info(
                "LLM 闸门排队 | user=%s label=%s 位次=%d running=%d",
                user_id, label, self.position(ticket), self._running,
            )
        return ticket

    async def wait(self, ticket: Ticket) -> None:
        """阻塞等到拿到名额；超时抛 429。非流式路径用。"""
        if ticket.granted:
            return
        remaining = max(0.0, ticket.deadline - time.monotonic())
        try:
            await asyncio.wait_for(asyncio.shield(ticket.future), remaining)
        except asyncio.TimeoutError:
            raise ServiceBusy(
                f"排队等待超过 {self.wait_timeout:.0f} 秒仍未轮到，请稍后再试。",
                "queue_timeout",
            )
        ticket.granted = True

    def release(self, ticket: Ticket) -> None:
        """归还名额。幂等，可以在多处 finally 里重复调。"""
        if ticket.released:
            return
        ticket.released = True

        left = self._user_active.get(ticket.user_id, 1) - 1
        if left > 0:
            self._user_active[ticket.user_id] = left
        else:
            self._user_active.pop(ticket.user_id, None)

        if not ticket.holds_slot:
            # 还在队列里（超时 / 客户端断连）→ 摘掉即可，没占名额
            if ticket.future is not None:
                try:
                    self._waiters.remove(ticket)
                except ValueError:
                    pass
                if not ticket.future.done():
                    ticket.future.cancel()
            return
        self._hand_off()

    def _hand_off(self) -> None:
        """把刚腾出的名额直接过户给队首；没人排队才真正减少 running。"""
        while self._waiters:
            nxt = self._waiters.popleft()
            if nxt.future is None or nxt.future.done():
                continue  # 已超时/取消，跳过
            nxt.holds_slot = True
            nxt.future.set_result(True)
            return
        self._running -= 1

    # ── SSE 包装 ────────────────────────────────────────────────────────────
    def wrap_stream(
        self,
        ticket: Ticket,
        factory: Callable[[], AsyncIterator[str]],
        *,
        notify_every: float = 3.0,
    ) -> AsyncIterator[str]:
        """把 SSE 生成器裹进闸门，返回可直接塞给 StreamingResponse 的生成器。

        故意写成**普通函数**而不是 async generator：`adopted` 必须在路由 return 之前
        同步置位。FastAPI 的依赖 AsyncExitStack 在 return response 之前就关闭了
        （见 fastapi/routing.py `get_request_handler`），置晚一步名额就被提前还回去。
        """
        ticket.adopted = True
        return self._stream_impl(ticket, factory, notify_every)

    async def _stream_impl(
        self,
        ticket: Ticket,
        factory: Callable[[], AsyncIterator[str]],
        notify_every: float,
    ) -> AsyncIterator[str]:
        """排队期间推 `queued` 帧，拿到名额后委托给 factory()，收尾一定归还名额。

        排队超时改不了 HTTP 状态码（响应头早发出去了），所以降级成 `error` + `done` 帧，
        前端按既有的 error 分支提示即可。
        """
        try:
            if not ticket.granted:
                yield _sse("queued", self._queue_payload(ticket))
                while True:
                    try:
                        await asyncio.wait_for(asyncio.shield(ticket.future), notify_every)
                        break
                    except asyncio.TimeoutError:
                        if time.monotonic() >= ticket.deadline:
                            yield _sse("error", {
                                "code": "queue_timeout",
                                "message": f"排队等待超过 {self.wait_timeout:.0f} 秒仍未轮到，请稍后再试。",
                            })
                            yield _sse("done", {})
                            return
                        yield _sse("queued", self._queue_payload(ticket))
                ticket.granted = True
            # 流生成器可能跑在与依赖不同的上下文里，这里补一次归属（get_current_user 已设过一次）
            set_usage_owner(ticket.user_id)
            async for chunk in factory():
                yield chunk
        finally:
            self.release(ticket)

    def _queue_payload(self, ticket: Ticket) -> dict:
        return {
            "position": self.position(ticket),
            "running": self._running,
            "waiting": len(self._waiters),
            "message": "当前使用人数较多，已进入排队…",
        }


def _build_gate() -> LLMGate:
    s = get_settings()
    return LLMGate(
        limit=s.llm_max_concurrency,
        per_user=s.llm_max_concurrency_per_user,
        queue_size=s.llm_queue_size,
        wait_timeout=s.llm_queue_timeout_seconds,
        per_user_grace=s.llm_per_user_grace_seconds,
    )


llm_gate = _build_gate()


# ── 配额 ──────────────────────────────────────────────────────────────────────

async def enforce_daily_quota(user: User, db: AsyncSession) -> None:
    """当天 token 用尽就拦下。配额 ≤ 0 表示不限；管理员默认豁免。"""
    s = get_settings()
    quota = s.daily_token_quota
    if quota <= 0:
        return
    if s.quota_exempt_admins and is_admin(user):
        return
    used = await get_today_tokens(db, user.id)
    if used >= quota:
        raise ServiceBusy(
            f"今日 token 配额已用完（{used:,}/{quota:,}），次日 00:00 重置。",
            "daily_quota",
        )


# ── 后台任务 ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def background_slot(label: str, *, wait_timeout: float | None = None):
    """后台 LLM 任务用的名额。取不到就直接放弃——后台活儿不值得把队列堵住。

    与用户请求共用同一个全局闸门，所以高峰期后台巡检自然排在真人后面。
    """
    try:
        ticket = await llm_gate.reserve(BACKGROUND_USER_ID, label)
    except ServiceBusy:
        logger.info("后台任务 %s 未取到闸门名额，本次跳过", label)
        raise
    try:
        if wait_timeout is not None:
            ticket.deadline = time.monotonic() + wait_timeout
        await llm_gate.wait(ticket)
        yield ticket
    finally:
        llm_gate.release(ticket)


# ── FastAPI 依赖 ──────────────────────────────────────────────────────────────

async def llm_ticket(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AsyncIterator[Ticket]:
    """**流式**路由用：校验配额 + 取号，但**不等待**。

    路由体校验完自己的业务前置条件后，把原生成器交给 `llm_gate.wrap_stream(ticket, events)`，
    排队进度就会以 `queued` 帧推给前端。没被 wrap_stream 接管的号（比如路由体中途抛了
    404）由这里的 teardown 兜底归还。

    注意：取号发生在依赖阶段，也就是在路由体的业务校验**之前**——所以「一个账号只能有一个
    任务」这条会把「同时发起的非法请求」也算进去，但它们几毫秒内就会抛错并归还，无影响。
    """
    await enforce_daily_quota(user, db)
    ticket = await llm_gate.reserve(user.id, request.url.path)
    try:
        yield ticket
    finally:
        if not ticket.adopted:
            llm_gate.release(ticket)


async def llm_slot(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AsyncIterator[Ticket]:
    """**非流式**路由用：校验配额 + 占名额（必要时阻塞排队），请求结束自动归还。

    路由体一行都不用改。排队期间客户端就是普通的「请求还没返回」，等超时会收到 429。
    """
    await enforce_daily_quota(user, db)
    ticket = await llm_gate.reserve(user.id, request.url.path)
    try:
        await llm_gate.wait(ticket)
        yield ticket
    finally:
        if not ticket.adopted:
            llm_gate.release(ticket)
