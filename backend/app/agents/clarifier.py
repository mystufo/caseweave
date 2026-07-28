"""Clarifier Agent: analyzes document chunks and generates clarification questions."""
import logging
import re
import time
from typing import Any
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage
from app.agents.llm_factory import build_chat_model
from app.agents._prompt_dump import dump_prompt, dump_response
from app.config import get_settings

logger = logging.getLogger("caseweave.clarifier")

CASE_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,39}$")


def _sanitize_case_prefix(value: Any) -> str:
    """Coerce LLM-provided prefix into a valid uppercase A–Z/0–9/- token, fallback to CASE."""
    if not isinstance(value, str):
        return "CASE"
    cleaned = value.strip().upper()
    if cleaned.startswith("TC-"):
        cleaned = cleaned[3:]
    # Convert underscores and other separators to dashes
    cleaned = re.sub(r"[^A-Z0-9-]", "-", cleaned).strip("-")
    cleaned = re.sub(r"-+", "-", cleaned)
    if not cleaned or not cleaned[0].isalpha():
        return "CASE"
    if len(cleaned) > 40:
        cleaned = cleaned[:40].rstrip("-")
    return cleaned if CASE_PREFIX_RE.match(cleaned) else "CASE"

SYSTEM_PROMPT = """你是一位资深的测试工程师，正在分析产品需求文档以准备编写测试用例。

你的任务是：识别文档中存在歧义、缺失或不确定的地方，生成一份澄清问题列表，并基于文档内容给出**用例编号前缀的建议值**。

识别策略：
1. 文档中使用了"可能"、"建议"、"待定"、"TBD"等模糊词汇
2. 缺少边界条件描述（最大值、最小值、特殊字符处理等）
3. 涉及与其他模块交互但未说明交互细节
4. 异常场景和错误处理未描述
5. 文档中有逻辑矛盾或前后不一致

## case_prefix_suggestion 生成规则（必须遵守）
- 基于文档主题/模块名翻译成英文（必要时使用业界通用缩写），形成简洁可读的前缀
- 仅允许 **大写英文字母 A–Z、数字 0–9、短横线 -**，必须以字母开头
- 多个英文单词之间用短横线连接，例如：USER-LOGIN / VIP-RENEWAL / ORDER-REFUND / SMS-VERIFY / CART-CHECKOUT
- 总长度建议 5–30 字符，不允许出现 `TC-`、下划线、空格、中文、拼音首字母混拼
- 这一字段必须给出，不允许为空字符串

## 每个问题必须给出候选答案 options（极重要）
- 为每个 question 提供 2–4 条覆盖**主流取值**的候选答案，方便用户直接点选；不要让用户从零编写
- 候选答案要**互斥、具体、可直接作为最终回答**（避免"待定/视情况而定"等占位词）
- 候选答案应包括"主流方案"和"常见替代方案"，例如：
  - 边界类问题：给出几个典型阈值（如「6 位」「8 位」「10 位以上」）
  - 异常处理类：给出常见处理策略（如「弹窗提示并保留输入」「页面跳转到错误页」「静默忽略」）
  - 业务规则类：给出主要分支（如「全部用户都允许」「仅 VIP 允许」「按等级分级允许」）
- options **不要**包含"其他/自定义"——前端会自动加这一项
- 候选项每条应在 25 字以内，长度均衡

输出格式（JSON）：
{
  "summary": "文档内容简要摘要",
  "module_detected": "检测到的功能模块名称，必须为纯中文，不允许含英文/拼音",
  "case_prefix_suggestion": "建议的用例编号前缀（必须为大写英文，多个单词以短横线连接，例如 USER-LOGIN / VIP-RENEWAL）",
  "questions": [
    {
      "id": 1,
      "category": "边界条件|异常处理|模块交互|业务规则|UI行为",
      "question": "具体问题描述",
      "context": "文档中触发此问题的原文片段",
      "importance": "high|medium|low",
      "options": ["候选答案1", "候选答案2", "候选答案3"]
    }
  ],
  "ready_to_generate": false
}

如果文档描述足够清晰，无需澄清，则将 ready_to_generate 设为 true，questions 为空数组（case_prefix_suggestion 仍必须给出）。
只输出JSON，不要有其他说明文字。

## 输入说明（重要）
- 用户可能给出"产品需求文档（PRD）"和/或"测试脑图"两份资料。脑图代表测试人员对 PRD 二次梳理后的最终测试意图。
- 当两者同时存在且描述冲突时，**以测试脑图为准**——脑图缺失的细节再回到 PRD 补齐。
- 仅有脑图时：基于脑图节点直接识别歧义，不要追问 PRD 才有的字段。
- 仅有 PRD 时：保持原有行为。"""


FOLLOWUP_SYSTEM_PROMPT = """你是一位资深的测试工程师，正在与产品/需求方进行多轮澄清，目的是在生成测试用例之前补齐所有关键细节。

你已经针对文档提出过若干澄清问题，并拿到了用户的回答。现在请你结合「文档 + 已澄清问答记录」做一次更深入的判断：

判断要点：
1. 用户的回答是否引入了**新的歧义、新的边界、新的异常分支**？（例如用户说"超过10次锁定"，那"锁定多久解除"就是新问题）
2. 用户的回答是否有**自相矛盾**或与文档冲突，需要再次确认？
3. 用户是否回避或敷衍了某个关键问题（例如只答"待定"/"看情况"），需要再追问一次以拿到可执行答案？
4. 现有信息组合后，是否仍有**测试用例必须明确而文档/回答都没提**的关键点？

输出原则（严格遵守）：
- 仅当**确实还有阻塞用例编写的关键歧义**时才提出新问题；避免吹毛求疵和重复已澄清的内容
- 不要重复之前轮次已经问过且已被回答的问题
- 新问题要具体、可回答，且对测试用例有实际影响
- 如果没有新的阻塞点，直接 `ready_to_generate=true` 并返回空 `questions`

## 每个新问题必须给出候选答案 options（与第一轮一致）
- 为每个 question 提供 2–4 条互斥、具体的候选答案，覆盖主流取值
- 候选项应基于本轮已掌握的上下文给出更贴合的取值（避免与用户已答内容重复）
- options **不要**包含"其他/自定义"——前端会自动加这一项
- 候选项每条 25 字以内

输出格式（JSON，字段含义与第一轮一致）：
{
  "summary": "结合用户回答后的最新理解",
  "module_detected": "纯中文模块名（保持与之前一致即可）",
  "case_prefix_suggestion": "保持与之前一致（大写英文+短横线）",
  "questions": [
    {
      "id": 1, "category": "...", "question": "...",
      "context": "触发追问的回答或文档片段",
      "importance": "high|medium|low",
      "options": ["候选答案1", "候选答案2"]
    }
  ],
  "ready_to_generate": false
}

只输出 JSON，不要有其他说明文字。"""


def _build_llm() -> BaseChatModel:
    # max_tokens 由 .env 的 CLARIFIER_MAX_TOKENS 控制（默认 8192）。
    return build_chat_model(
        max_tokens=get_settings().clarifier_max_tokens,
        temperature=0,
    )


def _build_clarifier_messages(
    doc_content: str,
    module_name: str | None,
    existing_knowledge: str | None,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
):
    user_content = f"产品模块：{module_name or '未指定'}\n\n"
    if existing_knowledge:
        user_content += f"已有知识库摘要：\n{existing_knowledge}\n\n"
    # 脑图放在 PRD 之前，提示 LLM 优先以脑图为准（与 system prompt 中的"以脑图为准"呼应）
    if mindmap_content:
        user_content += f"## 测试脑图（与 PRD 冲突时以脑图为准）\n{mindmap_content}\n\n"
    if doc_content:
        user_content += f"## 需求文档\n{doc_content}"
    return [
        SystemMessage(content=system_prompt or SYSTEM_PROMPT),
        HumanMessage(content=user_content),
    ], user_content


def _parse_clarifier_response(raw: str, module_name: str | None) -> dict[str, Any]:
    import json

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Clarifier JSON parse failed (%s); raw response truncated to 400 chars: %s",
            exc,
            cleaned[:400],
        )
        return {
            "summary": "文档解析完成",
            "module_detected": module_name or "未知模块",
            "case_prefix_suggestion": "CASE",
            "questions": [],
            "ready_to_generate": True,
            "raw_response": cleaned,
        }

    parsed["case_prefix_suggestion"] = _sanitize_case_prefix(parsed.get("case_prefix_suggestion"))

    # 规范化每个 question 的 options：剔除空白 / 去重 / 截断长度，缺字段给 []
    for q in parsed.get("questions") or []:
        raw_opts = q.get("options")
        cleaned: list[str] = []
        if isinstance(raw_opts, list):
            seen: set[str] = set()
            for opt in raw_opts:
                if not isinstance(opt, str):
                    continue
                s = opt.strip()
                if not s or s in seen:
                    continue
                if len(s) > 60:
                    s = s[:60]
                seen.add(s)
                cleaned.append(s)
        q["options"] = cleaned[:4]
    return parsed


async def analyze_document_for_clarification(
    doc_content: str,
    module_name: str | None = None,
    existing_knowledge: str | None = None,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Analyze document content and return clarification questions (non-streaming)."""
    llm = _build_llm()
    active_system = system_prompt or SYSTEM_PROMPT
    messages, user_content = _build_clarifier_messages(
        doc_content, module_name, existing_knowledge, mindmap_content, active_system,
    )

    logger.info(
        "Clarifier LLM call | module=%s doc_chars=%d mindmap_chars=%d prompt_chars=%d",
        module_name or "未指定",
        len(doc_content or ""),
        len(mindmap_content or ""),
        len(user_content),
    )
    dump_path = dump_prompt(
        agent="clarifier_initial",
        system=active_system,
        user=user_content,
        extra={
            "module": module_name or "未指定",
            "doc_chars": len(doc_content or ""),
            "mindmap_chars": len(mindmap_content or ""),
            "has_knowledge": bool(existing_knowledge),
        },
    )
    start = time.perf_counter()
    response = await llm.ainvoke(messages)
    elapsed_ms = (time.perf_counter() - start) * 1000
    raw = response.content.strip()
    logger.info("Clarifier LLM responded | response_chars=%d (%.0fms)", len(raw), elapsed_ms)
    dump_response(dump_path, raw)

    return _parse_clarifier_response(raw, module_name)


async def stream_analyze_document_for_clarification(
    doc_content: str,
    module_name: str | None = None,
    existing_knowledge: str | None = None,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
):
    """
    Stream Clarifier output token-by-token.
    Yields tuples (kind, payload):
      - ("token", str): incremental LLM text
      - ("result", dict): final parsed JSON (yielded once at the end)
    """
    llm = _build_llm()
    active_system = system_prompt or SYSTEM_PROMPT
    messages, user_content = _build_clarifier_messages(
        doc_content, module_name, existing_knowledge, mindmap_content, active_system,
    )

    logger.info(
        "Clarifier LLM stream | module=%s doc_chars=%d mindmap_chars=%d prompt_chars=%d",
        module_name or "未指定",
        len(doc_content or ""),
        len(mindmap_content or ""),
        len(user_content),
    )
    dump_path = dump_prompt(
        agent="clarifier_initial_stream",
        system=active_system,
        user=user_content,
        extra={
            "module": module_name or "未指定",
            "doc_chars": len(doc_content or ""),
            "mindmap_chars": len(mindmap_content or ""),
            "has_knowledge": bool(existing_knowledge),
        },
    )
    start = time.perf_counter()
    buffer = ""
    async for chunk in llm.astream(messages):
        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if not text:
            continue
        buffer += text
        yield ("token", text)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("Clarifier LLM stream done | response_chars=%d (%.0fms)", len(buffer), elapsed_ms)
    dump_response(dump_path, buffer)

    yield ("result", _parse_clarifier_response(buffer, module_name))


def _build_followup_messages(
    doc_content: str,
    module_name: str | None,
    case_prefix: str | None,
    rounds_history: list[dict[str, Any]],
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
):
    """rounds_history: ordered list of {"questions":[...], "answers":{id:str}} per past round."""
    lines = [f"产品模块：{module_name or '未指定'}"]
    if case_prefix:
        lines.append(f"用例编号前缀：{case_prefix}")
    lines.append("")
    if mindmap_content:
        lines.append("## 测试脑图（与 PRD 冲突时以脑图为准）")
        lines.append(mindmap_content)
        lines.append("")
    if doc_content:
        lines.append(f"需求文档内容：\n{doc_content}")
        lines.append("")
    lines.append("## 已完成的澄清轮次")
    for i, rnd in enumerate(rounds_history, start=1):
        lines.append(f"### 第 {i} 轮")
        for q in rnd.get("questions") or []:
            qid = str(q.get("id"))
            answer = (rnd.get("answers") or {}).get(qid, "")
            lines.append(f"- Q（{q.get('category', '')}|{q.get('importance', '')}）：{q.get('question', '')}")
            if q.get("context"):
                lines.append(f"  原文：{q['context']}")
            lines.append(f"  A：{answer or '（用户未作答）'}")
        lines.append("")
    lines.append("请基于以上信息判断是否还需要再追问。若无，请将 ready_to_generate 设为 true 并返回空 questions。")
    user_content = "\n".join(lines)
    return [
        SystemMessage(content=system_prompt or FOLLOWUP_SYSTEM_PROMPT),
        HumanMessage(content=user_content),
    ], user_content


async def stream_followup_clarification(
    doc_content: str,
    module_name: str | None,
    case_prefix: str | None,
    rounds_history: list[dict[str, Any]],
    round_index: int,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
):
    """
    Stream a follow-up clarification round.
    Yields ("token", str) chunks then ("result", dict) at the end.
    """
    llm = _build_llm()
    active_system = system_prompt or FOLLOWUP_SYSTEM_PROMPT
    messages, user_content = _build_followup_messages(
        doc_content, module_name, case_prefix, rounds_history, mindmap_content, active_system,
    )

    logger.info(
        "Clarifier follow-up stream | round=%d module=%s prefix=%s doc_chars=%d mindmap_chars=%d prompt_chars=%d history=%d",
        round_index,
        module_name or "未指定",
        case_prefix or "",
        len(doc_content or ""),
        len(mindmap_content or ""),
        len(user_content),
        len(rounds_history),
    )
    dump_path = dump_prompt(
        agent=f"clarifier_followup_r{round_index}",
        system=active_system,
        user=user_content,
        extra={
            "round": round_index,
            "module": module_name or "未指定",
            "case_prefix": case_prefix or "",
            "doc_chars": len(doc_content or ""),
            "mindmap_chars": len(mindmap_content or ""),
            "history_rounds": len(rounds_history),
        },
    )
    start = time.perf_counter()
    buffer = ""
    async for chunk in llm.astream(messages):
        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if not text:
            continue
        buffer += text
        yield ("token", text)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("Clarifier follow-up done | round=%d chars=%d (%.0fms)", round_index, len(buffer), elapsed_ms)
    dump_response(dump_path, buffer)

    result = _parse_clarifier_response(buffer, module_name)
    # Preserve the user-confirmed prefix across follow-up rounds so the suggestion stays stable.
    if case_prefix:
        result["case_prefix_suggestion"] = case_prefix
    yield ("result", result)
