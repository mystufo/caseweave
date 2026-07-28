"""Mindmap Generator Agent: 把产品需求文档提炼成 Markdown 大纲格式的「测试脑图」。

与 generator.py 的区别：generator 产出可执行用例 JSON；本 agent 产出更上层的
**测试脑图**——按功能的真实结构与交互逻辑重建成的思维大纲。

组织原则（关键）：以「功能结构 + 交互逻辑」为主干，而非按「正向/反向/边界」等测试
分类平铺。从入口/触发点出发，顺着用户操作流与系统响应逐层下钻，父子节点承载明确的
逻辑语义（包含 / 触发跳转 / 前置条件→结果 / 状态→分支），显式表达功能间的依赖与联动；
测试覆盖（正常/异常/边界/校验）就近挂在对应功能节点下，而不是抽成顶层大类。目的是让
测试同学先看懂功能逻辑，再据此写用例。

产物是纯 Markdown 大纲（单 `#` 根 + 多级 `-` 缩进列表），写入飞书文档后可一键切换
「大纲 / 思维导图」视图。
"""
import logging
import time

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage

from app.agents.llm_factory import build_chat_model
from app.agents._prompt_dump import dump_prompt, dump_response
from app.config import get_settings

logger = logging.getLogger("caseweave.mindmap_generator")

SYSTEM_PROMPT = """你是一位资深的测试工程师，负责把产品需求文档（PRD）梳理成一份「测试脑图」。
测试脑图的核心价值不是罗列测试点，而是**把 PRD 里描述的功能，按其真实的结构与交互逻辑重建成一棵树**——
让测试同学顺着这棵树，就能看懂「这个功能由哪些部分组成、每一步会触发什么、什么条件下走哪个分支、各状态之间如何流转、彼此如何依赖」，
在此基础上再去写用例。所以脑图要还原的是**逻辑关系与依赖**，而不是把需求条目机械地摊平。

## 组织原则（本脑图的灵魂，务必遵守）
**以「功能结构 + 交互逻辑」为主干来组织，而不是以「正向/反向/边界」这类测试分类为主干。**
- ❌ 不要先分出「正向流程 / 反向与异常 / 边界值 / …」几个大类、再往每类里平铺测试点——
  那样会把同一个功能拆散到多个类别里，读者无法围绕一个功能形成完整心智模型，逻辑关系也丢失了。
- ✅ 要从**入口/触发点**出发，顺着**用户操作流与系统响应**逐层下钻，镜像产品/界面的真实层级：
  入口 → 触发的界面或弹窗 → 界面里的元素与子功能 → 每个元素的状态 → 每个状态下的行为分支/跳转 → 更深层的子流程（如多步向导）。
- 父子关系必须承载明确语义，是下面四种之一：
  1. **包含**：A 由 B、C 组成（界面 → 它的各个区块/元素）。
  2. **触发/跳转**：做了 A 会打开/跳转到/弹出 B（点击按钮 → 打开某弹窗）。
  3. **前置条件 → 结果**：满足条件 C 才能做 D；不满足时表现为 E（选项未选 → 下一步按钮置灰）。
  4. **状态 → 分支**：处于某状态时表现如何（任务已完成 / 未完成 各自的样子；成功 / 失败 / 处理中）。

## 必须显式表达的逻辑关系（这是重点，缺了就等于没梳理）
- **触发与跳转**：某操作会打开哪个界面、跳到哪个 tab、弹出哪个弹窗。
- **前置条件与校验**：满足什么条件才可点击/可进入下一步；不满足时的表现（按钮置灰、错误提示、无响应）。
- **状态与流转**：已完成/未完成、成功/失败/处理中/排队中，以及状态何时切换、切换后各处表现的差异（含重置时机等）。
- **条件分支与适用范围**：哪些情况适用、哪些不适用（例如「通用/人像/纹理 显示引导入口；分割/浮雕 不显示」）。
- **联动与依赖**：一个功能的结果如何影响其他功能或模块（积分到账 → 影响消耗中心展示；新规则取代旧规则）。

## 保留 PRD 里的具象细节（测试可验证的锚点，不要抽象掉）
确切文案（标题/描述/toast/按钮文字）、数值与阈值（积分数、字数上限、次数上限、有效期如 T+30）、
关键时间点（如 UTC 0 点重置）、具体选项内容——凡 PRD 写明的，都尽量落到对应节点上。

## 测试覆盖如何体现（融进功能树，不要单列大类）
- 正常路径、异常/失败、边界/空值/特殊字符、校验规则，都**就近挂在对应功能节点下面**，
  作为该功能的子节点自然出现，而不是抽到顶层的「反向」「边界」大类里。
- 只有确实**不属于任何单个功能**的跨功能关注点，才在最后单独收一个分支，例如
  「全局与跨功能」下放：兼容性/性能、权限与防刷、跨模块联动影响、数据埋点上报等。

## 粒度与深度
- 凡 PRD 描述了子结构、子步骤、子状态的地方都要展开、逐层下钻，不要压平成一行
  （例如多步调研向导，要一步步展开每步的标题、选项、校验、上一步/下一步/提交/关闭确认）。
- 叶子节点是一个可独立验证的点，措辞简洁；但中间节点要清楚表达它与父/子的逻辑关系。
- 贴合真实业务，不臆造 PRD 中不存在的功能。节点用中文（保留 PRD 原文里的英文文案）。

## 输出格式（严格遵守）
只输出 **Markdown 大纲**，不要任何解释性文字、不要代码围栏（```）。
- `#` 一级标题：唯一的根节点，写功能模块名（由用户给定）。
- 其余全部用 `-` 列表，靠**多级缩进**（每级 2 个空格）表达功能树的父子逻辑关系，可以嵌套很多层。
- 不要用 `##`/`###` 把内容切成测试分类；层级一律靠缩进列表表达。

## 示例（仅示意「按功能逻辑组织」的形态，实际以 PRD 内容为准）
# 任务中心
- 入口：Header 礼物按钮
  - 展示条件：用户登录后才显示
  - 有未完成任务时显示红点；全部完成后红点消失
  - hover 提示文案「Earn free credits...」
  - 点击 → 居中弹出任务中心弹窗
- 任务中心弹窗
  - 打开入口
    - 首页点击礼物按钮
    - 模型生成过程中的引导入口
      - 通用/人像/纹理 生成时显示「Explore Tasks」；分割/浮雕 不显示（适用范围分支）
  - 界面构成
    - 标题、描述文案（含「200+ 免费积分」）
    - 任务列表：逐个任务卡片（图标 / 标题说明 / 积分 / CTA 按钮）
  - 任务：解锁创作者画像（100 积分）
    - CTA 按钮
      - 已完成态：显示「已完成」，按钮置灰不可点
      - 未完成态：显示「Unlock Now」→ 点击弹出画像调研弹窗
        - 第一步（多选）：选项未选时「下一步」置灰；选「其他」须输入（≤100 字），否则仍不可点
        - 第二步（单选）：可返回上一步；未选时「下一步」置灰
        - …（逐步展开各步的校验与前进/后退逻辑）
        - 提交 → 关闭弹窗并弹 toast；中途关闭需二次确认，确认后下次从头开始
  - 完成任务 toast：登录、解锁画像有专属文案，其余通用「恭喜获得 N 积分」
- 全局与跨功能
  - 积分联动：任务积分到账后在积分详情页展示，有效期统一 T+30
  - 防刷与权限：未登录不可访问；分享拉新每日上限 4 次
  - 兼容/性能：多浏览器、移动端弹窗、大并发下的状态同步

再次强调：以功能与交互逻辑为主干组织，显式表达触发/前置/状态/依赖关系；只输出 Markdown 大纲本身，不要额外说明、不要代码围栏。"""


def _build_llm() -> BaseChatModel:
    # 复用 generator 的 max_tokens 预算：脑图体量通常小于用例 JSON，但大 PRD 下节点也不少，
    # 且思考模型 max_tokens 含 reasoning，给足才不会正文空。
    return build_chat_model(
        max_tokens=get_settings().generator_max_tokens,
        temperature=0.3,
    )


def _strip_code_fence(raw: str) -> str:
    """剥掉模型偶尔包上的 ```markdown ... ``` 围栏，保留纯大纲。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        # 去掉首行围栏（可能是 ```markdown / ```md / ```）
        parts = s.split("```")
        # parts[0] 为空、parts[1] 是围栏内正文
        if len(parts) >= 2:
            body = parts[1]
            if body.startswith(("markdown", "md")):
                body = body.split("\n", 1)[1] if "\n" in body else ""
            return body.strip()
    return s


async def generate_mindmap(
    doc_content: str,
    module_name: str,
    relevant_knowledge: str | None = None,
    skills: str | None = None,
    clarification_answers: dict[str, str] | None = None,
    system_prompt: str | None = None,
) -> str:
    """把 PRD 正文提炼成 Markdown 大纲测试脑图，返回 Markdown 字符串。

    doc_content 应由调用方用 truncate_for_llm 截断后传入。
    clarification_answers：澄清阶段用户对每个问题的最终回答（{问题文本: 答案}），
    作为高优先级上下文注入——与文档默认理解冲突时以澄清结论为准。
    """
    llm = _build_llm()
    active_system = system_prompt or SYSTEM_PROMPT

    user_content = f"功能模块：{module_name}\n\n"

    # 澄清结论放最前，且明确其优先级高于文档默认理解（与 generator.py 的注入口径一致）。
    if clarification_answers:
        qa_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in clarification_answers.items())
        user_content += (
            "## 澄清结论（用户已确认，优先级高于文档默认理解，冲突时以此为准）\n"
            f"{qa_text}\n\n"
        )
    if skills:
        user_content += (
            "## 测试设计经验（来自该模块历史用例修改沉淀，应优先参考）\n"
            f"{skills}\n\n"
        )
    if relevant_knowledge:
        user_content += (
            "## 项目知识库（历史文档抽取，作为产品上下文参考；与当前文档冲突时以当前文档为准）\n"
            f"{relevant_knowledge}\n\n"
        )
    if doc_content:
        user_content += f"## 需求文档\n{doc_content}\n\n"

    user_content += (
        f"请根据以上信息，为「{module_name}」生成一份 Markdown 大纲格式的测试脑图。"
        "只输出 Markdown 大纲，不要代码围栏，不要额外说明。"
    )

    messages = [
        SystemMessage(content=active_system),
        HumanMessage(content=user_content),
    ]

    logger.info(
        "Mindmap LLM call | module=%s doc_chars=%d prompt_chars=%d",
        module_name, len(doc_content or ""), len(user_content),
    )
    dump_path = dump_prompt(
        agent="mindmap_generator",
        system=active_system,
        user=user_content,
        extra={"module": module_name, "doc_chars": len(doc_content or "")},
    )

    start = time.perf_counter()
    response = await llm.ainvoke(messages)
    elapsed_ms = (time.perf_counter() - start) * 1000
    raw = response.content if isinstance(response.content, str) else str(response.content)

    finish_reason = None
    try:
        meta = getattr(response, "response_metadata", {}) or {}
        finish_reason = (
            meta.get("finish_reason")
            or meta.get("stop_reason")
            or (meta.get("model_output", {}) or {}).get("finish_reason")
        )
    except Exception:
        pass
    logger.info(
        "Mindmap LLM responded | response_chars=%d finish=%s (%.0fms)",
        len(raw or ""), finish_reason, elapsed_ms,
    )
    dump_response(dump_path, raw, finish_reason=finish_reason)

    return _strip_code_fence(raw)
