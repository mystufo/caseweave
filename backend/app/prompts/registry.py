"""Prompt 版本化加载层（Phase 4.2 第一阶段）。

把原本硬编码在 agent 模块里的 system prompt 统一登记为可版本化的逻辑 key：
  - clarifier_initial   首轮澄清
  - clarifier_followup  续答澄清
  - generator           用例生成
  - mindmap_generator   测试脑图生成

代码里的常量是每个 key 的“原始建议版本”（default）。网页端允许用户基于它
另存为新版本并选择激活。运行时：
  get_active_prompt_text(db, project_id, key)
    → 取该项目 is_active=1 的最新版本；查不到则回退到 default 常量。

注意：default 文本从 agent 模块 import，agent 模块不反过来 import 本模块，
避免循环依赖（agent 函数改为接收 system_prompt 参数，由路由层注入）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.clarifier import SYSTEM_PROMPT as CLARIFIER_INITIAL_DEFAULT
from app.agents.clarifier import FOLLOWUP_SYSTEM_PROMPT as CLARIFIER_FOLLOWUP_DEFAULT
from app.agents.generator import SYSTEM_PROMPT as GENERATOR_DEFAULT
from app.agents.mindmap_generator import SYSTEM_PROMPT as MINDMAP_DEFAULT
from app.models.knowledge import PromptVersion

logger = logging.getLogger("testcraft.prompts")


@dataclass(frozen=True)
class PromptSpec:
    key: str          # 逻辑 key，存进 PromptVersion.prompt_id
    purpose: str      # clarification / generation
    label: str        # 网页端展示名
    description: str  # 网页端说明
    default_text: str # 原始建议版本


PROMPT_SPECS: list[PromptSpec] = [
    PromptSpec(
        key="clarifier_initial",
        purpose="clarification",
        label="澄清 · 首轮提问",
        description="分析 PRD/脑图，识别歧义并生成首轮澄清问题与用例编号前缀建议。",
        default_text=CLARIFIER_INITIAL_DEFAULT,
    ),
    PromptSpec(
        key="clarifier_followup",
        purpose="clarification",
        label="澄清 · 续答追问",
        description="结合已澄清问答，判断是否还需追问并生成新一轮问题。",
        default_text=CLARIFIER_FOLLOWUP_DEFAULT,
    ),
    PromptSpec(
        key="generator",
        purpose="generation",
        label="测试用例生成",
        description="根据文档 + 澄清结果生成结构化测试用例 JSON。",
        default_text=GENERATOR_DEFAULT,
    ),
    PromptSpec(
        key="mindmap_generator",
        purpose="mindmap",
        label="测试脑图生成",
        description="把 PRD 按功能结构与交互逻辑重建成脑图（入口→触发→界面→状态→分支），显式表达依赖与联动，测试覆盖就近挂在功能节点下。",
        default_text=MINDMAP_DEFAULT,
    ),
]

_SPEC_BY_KEY: dict[str, PromptSpec] = {s.key: s for s in PROMPT_SPECS}


def get_spec(key: str) -> PromptSpec | None:
    return _SPEC_BY_KEY.get(key)


def default_text(key: str) -> str:
    spec = _SPEC_BY_KEY.get(key)
    return spec.default_text if spec else ""


async def get_active_prompt_text(
    db: AsyncSession, project_id: int | None, key: str
) -> str:
    """返回该项目 key 的当前生效提示词；无激活版本时回退到代码默认常量。

    取数失败一律回退默认并 log warning——提示词加载绝不能阻塞核心生成流程。
    """
    fallback = default_text(key)
    if project_id is None:
        return fallback
    try:
        result = await db.execute(
            select(PromptVersion.template)
            .where(
                PromptVersion.project_id == project_id,
                PromptVersion.prompt_id == key,
                PromptVersion.is_active == 1,
            )
            .order_by(PromptVersion.id.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row:
            return row
    except Exception as exc:  # noqa: BLE001
        logger.warning("加载激活 prompt 失败 key=%s project=%s: %s", key, project_id, exc)
    return fallback


async def resolve_active_prompt(project_id: int | None, key: str) -> str:
    """自开 session 版的 get_active_prompt_text。

    供流式 SSE 处理器使用——那里外层请求 session 往往已随 StreamingResponse 关闭，
    各辅助函数都各自 `async with AsyncSessionLocal()` 取数。
    """
    if project_id is None:
        return default_text(key)
    from app.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            return await get_active_prompt_text(db, project_id, key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_active_prompt 取 session 失败 key=%s project=%s: %s", key, project_id, exc)
        return default_text(key)

