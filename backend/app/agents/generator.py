"""Generator Agent: produces structured test cases from doc + clarifications."""
import logging
import time
from typing import Any
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage
from app.agents.llm_factory import build_chat_model
from app.agents._prompt_dump import dump_prompt, dump_response
from app.config import get_settings

logger = logging.getLogger("caseweave.generator")

SYSTEM_PROMPT = """你是一位资深的测试工程师，负责根据产品需求文档和澄清结果编写完整的测试用例。

## 输入说明（重要）
- 用户可能给出"产品需求文档（PRD）"和/或"测试脑图"两份资料。脑图代表测试人员对 PRD 二次梳理后的最终测试意图。
- 当两者同时存在且描述冲突时，**以测试脑图为准**——脑图缺失的细节再回到 PRD 补齐。
- 仅有脑图时：直接以脑图节点为骨架生成用例，不要凭空补 PRD 才有的字段。
- 仅有 PRD 时：保持原有行为。

## 用例粒度（最重要的约束，务必严格遵守）
**一条测试用例 = 一个可独立验证的完整测试目标（一个"验证意图 + 判定点"），而不是一次 UI 操作、一个界面元素或一个步骤。**
- 一个连续的操作流程，只要它指向**同一个最终判定点**，就必须写成**一条**用例——把每一步操作依次写进 `steps` 字段（1. 2. 3. …），预期结果写这条流程走完后的最终验证点。
- **严禁**把同一流程的"第一步""第二步""第三步"、或"点击打开→逐步填写→提交"这类顺序操作拆成多条用例。
- **严禁**把"查看标题""查看描述""查看列表"这种同一界面的多个静态元素各写成一条用例；应合并为"某界面元素展示正确"一条，steps 逐条查看，预期结果逐条列出。
- 只有当操作对应**不同且可独立判定的预期结果**时才拆成不同用例（例如：不同的正向路径、各自独立的反向/边界判定点、彼此互斥的分支结果）。
- 判断口诀：如果两条"用例"必须**按顺序连续执行、后者依赖前者的操作状态、且中间没有各自独立的判定意图**，那它们本就是同一条用例的多个步骤，必须合并。

### 粒度正反例（以"用户画像调研弹窗"为例）
- ❌ 错误：调研第一步选择 / 调研第二步选择 / 调研第三步选择 / 点击提交……各拆成一条用例。
- ✅ 正确：`完成用户画像调研并提交` 一条正向用例——steps 覆盖"打开弹窗→第一步选择→第二步选择→第三步选择→第四步选择→点击提交"，expected_result 用序号对应各步骤的判定点（如 `6. 调研完成、任务标记完成并弹出 toast`）。
- ✅ 仍可独立的反向/边界用例：`某步未选择时下一步按钮置灰`、`其他输入框超 100 字截断` 等——因为它们各自有**独立的判定点**，与主流程判定不同。

## 测试用例覆盖策略
1. **正向用例**：覆盖所有正常业务流程（Happy Path）
2. **反向用例**：边界值、异常输入、权限不足、网络异常等
3. **关联用例**：基于模块关联关系，生成跨功能交互场景
4. **边界用例**：最大值、最小值、空值、特殊字符

## 输出格式（JSON数组）
输出一个JSON数组，每个元素为一条测试用例：
[
  {
    "case_number": "{CASE_PREFIX}-{SUB}-001",
    "name": "用例名称（简洁描述测试意图）",
    "module": "功能模块名称",
    "priority": "P1",
    "preconditions": "前置条件（执行前需满足的条件）",
    "steps": "1. 步骤一\\n2. 步骤二\\n3. 步骤三",
    "expected_result": "1. 步骤一对应的预期结果\\n2. 步骤二对应的预期结果\\n3. 步骤三对应的预期结果",
    "remarks": "备注或注意事项（可为空）",
    "test_result": ""
  }
]

## 执行步骤与预期结果的对应规则（务必遵守）
- `expected_result` 可以有多条，且**必须与 `steps` 里需要校验的步骤序号一一对应**：预期结果每一条以对应的步骤序号开头（如 `2. …` 表示这是第 2 步的预期结果）。
- 只对**需要产生可观察结果/需要断言**的步骤写预期结果；纯准备性、无观察点的操作步骤可不写对应预期结果（序号跳过即可）。
- 序号必须真实指向 `steps` 中存在的步骤，不得错位、不得凭空多出步骤里没有的序号。
- 若整条用例只有一个最终判定点，可只写一条预期结果并标注其对应的步骤序号（如 `3. 提交成功并弹出 toast`）。
- 示例：
  - steps: `1. 打开调研弹窗\\n2. 第一步选择"个人用途"\\n3. 第四步点击提交`
  - expected_result: `1. 弹窗居中展示，标题正确\\n2. "下一步"按钮高亮可点\\n3. 调研完成、任务标记完成并弹出 toast`

## 优先级评定规则（priority 字段，必填）
- **P1（最高）**：核心主流程、影响整体可用性、阻塞业务的功能点；登录/支付/下单/数据安全/权限校验等关键正向用例
- **P2（中）**：常用功能、典型异常分支、主要边界场景；非核心但高频的业务路径
- **P3（最低）**：极端边界、低频异常、UI 细节、辅助提示文案、兜底兼容场景
- 必须从 P1/P2/P3 中三选一，不可省略，不可写其他值

## 用例编号规则（严格遵守）
- 必须使用统一前缀，格式：`{CASE_PREFIX}-{SUB}-{3位序号}`（不要加 `TC-` 这种额外前缀）
- `{CASE_PREFIX}` 由用户给定（其内部可能已经包含短横线，如 `USER-LOGIN`），所有用例的此段必须完全一致，并出现在 case_number 最开头
- `{SUB}` 是你针对该用例所属子功能/场景补充的英文短词（大写、可选，可省略时输出 `{CASE_PREFIX}-001` 形式）；同一子场景下用例的 `{SUB}` 必须保持一致
- 例如前缀 USER-LOGIN：USER-LOGIN-VALID-001 / USER-LOGIN-INVALID-001 / USER-LOGIN-LOCKOUT-001

## 要求
- 每个功能点至少生成1条正向用例 + 2条反向用例
- 再次强调粒度：同一操作流程的连续步骤必须合并进一条用例的 `steps`，不要按步骤拆分用例（见上文"用例粒度"约束）
- 步骤要具体可执行，不能有"等操作"这类模糊描述
- 预期结果要明确，包含具体的提示语、页面跳转、数据变化等；多条预期结果需按步骤序号与 steps 一一对应（见"执行步骤与预期结果的对应规则"）
- 只输出JSON数组，不要有其他说明文字"""


def _build_llm() -> BaseChatModel:
    # max_tokens 由 .env 的 GENERATOR_MAX_TOKENS 控制（默认 16384）。
    # 脑图 + PRD 同时输入时输出体量大；思考模型的 max_tokens 含 thinking，
    # 给小了会让正文被截断成 0 条
    return build_chat_model(
        max_tokens=get_settings().generator_max_tokens,
        temperature=0.2,
    )


def _salvage_truncated_json_array(raw: str) -> list | None:
    """
    模型把 JSON 数组写到一半就被 max_tokens 截断的兜底解析。
    策略：从尾部往回找最后一个完整的 `}`（top-level 大括号），把那之后裁掉，
    再补 `]` 闭合数组——能救回 N-1 条已成形的用例，比直接返回 [] 强。
    """
    s = (raw or "").strip()
    if not s.startswith("["):
        return None
    depth_brace = 0
    in_str = False
    escape = False
    last_close = -1
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
            if depth_brace == 0:
                last_close = i
    if last_close < 0:
        return None
    salvaged = s[: last_close + 1] + "]"
    try:
        import json as _json
        parsed = _json.loads(salvaged)
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None


async def generate_test_cases(
    doc_content: str,
    module_name: str,
    case_prefix: str,
    clarification_answers: dict[str, str] | None = None,
    skills: str | None = None,
    module_relations: str | None = None,
    relevant_knowledge: str | None = None,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """
    Generate structured test cases.
    Returns a list of test case dicts matching the JSON schema above.
    """
    import json

    llm = _build_llm()
    active_system = system_prompt or SYSTEM_PROMPT

    user_content = f"功能模块：{module_name}\n用例编号前缀（CASE_PREFIX，所有用例必须以 {case_prefix}- 开头，不要再加 TC- 之类的额外前缀）：{case_prefix}\n\n"

    # Skills 优先级最高：来自该模块历史用例修改沉淀的"测试设计经验"（人或 LLM 归纳的 Markdown 备忘单）。
    # 比项目知识库更"贴近测试意图"，所以注入位置在知识库之前。
    if skills:
        user_content += (
            "## 测试设计经验（来自该模块历史用例修改沉淀，应优先参考）\n"
            f"{skills}\n\n"
        )

    if relevant_knowledge:
        user_content += (
            "## 项目知识库（来自历史文档抽取，作为产品上下文参考；与当前文档冲突时以当前文档为准）\n"
            f"{relevant_knowledge}\n\n"
        )

    if module_relations:
        user_content += f"## 模块关联关系\n{module_relations}\n\n"

    # 脑图放在 PRD 之前，让 LLM 先看到测试人员的最终意图（system prompt 已声明冲突时以脑图为准）
    if mindmap_content:
        user_content += f"## 测试脑图（与 PRD 冲突时以脑图为准）\n{mindmap_content}\n\n"

    if doc_content:
        user_content += f"## 需求文档\n{doc_content}\n\n"

    if clarification_answers:
        qa_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in clarification_answers.items())
        user_content += f"## 澄清确认结果\n{qa_text}\n\n"

    user_content += f"请根据以上信息生成完整的测试用例列表。提醒：所有 case_number 必须以 `{case_prefix}-` 开头，不要带 `TC-` 前缀。"

    messages = [
        SystemMessage(content=active_system),
        HumanMessage(content=user_content),
    ]

    logger.info(
        "Generator LLM call | module=%s prefix=%s doc_chars=%d mindmap_chars=%d answers=%d prompt_chars=%d",
        module_name,
        case_prefix,
        len(doc_content or ""),
        len(mindmap_content or ""),
        len(clarification_answers or {}),
        len(user_content),
    )
    dump_path = dump_prompt(
        agent="generator",
        system=active_system,
        user=user_content,
        extra={
            "module": module_name,
            "case_prefix": case_prefix,
            "doc_chars": len(doc_content or ""),
            "mindmap_chars": len(mindmap_content or ""),
            "answers": len(clarification_answers or {}),
            "has_knowledge": bool(relevant_knowledge),
        },
    )
    start = time.perf_counter()
    response = await llm.ainvoke(messages)
    elapsed_ms = (time.perf_counter() - start) * 1000
    raw = response.content.strip()
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
        "Generator LLM responded | response_chars=%d finish=%s (%.0fms)",
        len(raw), finish_reason, elapsed_ms,
    )
    dump_response(dump_path, raw, finish_reason=finish_reason)

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        cases = json.loads(raw)
        if isinstance(cases, list):
            logger.info("Generator parsed %d cases", len(cases))
            return cases
        logger.warning("Generator returned non-list JSON: %s", str(cases)[:200])
        return []
    except json.JSONDecodeError as exc:
        # 最常见的失败模式：max_tokens 截断导致 JSON 末尾不完整。
        # 先尝试从尾部回滚到最后一个完整对象，能救回大部分用例
        salvaged = _salvage_truncated_json_array(raw)
        if salvaged:
            logger.warning(
                "Generator JSON truncated (%s); salvaged %d cases via tail-rollback "
                "(finish=%s, raw_len=%d, raw_head=%s)",
                exc, len(salvaged), finish_reason, len(raw), raw[:200],
            )
            return salvaged
        logger.warning(
            "Generator JSON parse failed (%s); finish=%s raw_len=%d head=%s tail=%s",
            exc, finish_reason, len(raw), raw[:300], raw[-300:],
        )
        return []


async def stream_generate_test_cases(
    doc_content: str,
    module_name: str,
    case_prefix: str,
    clarification_answers: dict[str, str] | None = None,
    skills: str | None = None,
    module_relations: str | None = None,
    relevant_knowledge: str | None = None,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
):
    """Stream-generate test cases token by token."""
    llm = _build_llm()
    active_system = system_prompt or SYSTEM_PROMPT

    user_content = f"功能模块：{module_name}\n用例编号前缀（CASE_PREFIX，所有用例必须以 {case_prefix}- 开头，不要再加 TC- 之类的额外前缀）：{case_prefix}\n\n"
    if skills:
        user_content += (
            "## 测试设计经验（来自该模块历史用例修改沉淀，应优先参考）\n"
            f"{skills}\n\n"
        )
    if relevant_knowledge:
        user_content += (
            "## 项目知识库（来自历史文档抽取，作为产品上下文参考；与当前文档冲突时以当前文档为准）\n"
            f"{relevant_knowledge}\n\n"
        )
    if module_relations:
        user_content += f"## 模块关联关系\n{module_relations}\n\n"
    if mindmap_content:
        user_content += f"## 测试脑图（与 PRD 冲突时以脑图为准）\n{mindmap_content}\n\n"
    if doc_content:
        user_content += f"## 需求文档\n{doc_content}\n\n"
    if clarification_answers:
        qa_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in clarification_answers.items())
        user_content += f"## 澄清确认结果\n{qa_text}\n\n"
    user_content += f"请根据以上信息生成完整的测试用例列表。提醒：所有 case_number 必须以 `{case_prefix}-` 开头，不要带 `TC-` 前缀。"

    messages = [
        SystemMessage(content=active_system),
        HumanMessage(content=user_content),
    ]
    dump_path = dump_prompt(
        agent="generator_stream",
        system=active_system,
        user=user_content,
        extra={
            "module": module_name,
            "case_prefix": case_prefix,
            "doc_chars": len(doc_content or ""),
            "mindmap_chars": len(mindmap_content or ""),
            "answers": len(clarification_answers or {}),
            "has_knowledge": bool(relevant_knowledge),
        },
    )

    buffer = ""
    async for chunk in llm.astream(messages):
        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        buffer += text
        yield chunk.content
    dump_response(dump_path, buffer)
