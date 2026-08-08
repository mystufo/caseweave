"""Fetch a Feishu/Lark document's **unresolved comments** via `lark-cli drive +list-comments`.

为什么要评论：需求文档的模糊点往往不在正文里，而在评论区的追问（「超时时间到底是 15 还是
30 分钟？」）。这些讨论对 Clarifier 判定歧义、对 Generator 补边界用例都有直接价值。

取全部评论（`--solved-status all`），已解决的也要。曾经默认只取未解决的，理由是「已解决说明
结论已回写正文」—— 实测证伪：某需求文档里两条已解决评论「完成邮箱验证：同时覆盖注册和登录」
「不区分注册和登录 都算作操作」，其结论在正文中出现 0 次。「已解决」的真实含义是「这个讨论有
结论了」，而不是「结论已同步到正文」。这类评论恰恰是确定性最高的需求决策，漏掉会直接导致用例
拆错路径。因此改为全取，用「已确认 / 讨论中」标注状态，让 LLM 自己区分权重。

局部评论自带 `quote`（评论锚定的那句原文），按 quote 分组后 LLM 能把讨论对上需求的具体位置，
比一串无锚点的流水账有用得多。

所有失败都吞掉转成 None —— 评论是增益信息，取不到不该拖垮整篇文档的导入。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import get_settings

logger = logging.getLogger("caseweave.lark_comments")

# 单条回复文本上限：评论区偶有人贴整段日志/报错，截断防止挤爆上下文。
_REPLY_MAX_CHARS = 500


async def fetch_document_comments(
    url: str,
    *,
    doc_token: str | None = None,
    timeout: float | None = None,
    identity: str | None = None,
) -> str | None:
    """把文档的未解决评论取成一段 markdown 文本；无评论或取不到时返回 None。

    doc_token 是 docx 的 document_id。**强烈建议传**：走 `--url` 时 lark-cli 需要先把 wiki
    链接解析成 docx token，而这一步要 `wiki:wiki` / `wiki:node:read` 等额外 scope（bot 身份
    默认没有）；直接给 token 就绕开了整个 wiki 解析，实测 bot 无需任何补授权即可读评论。
    正文抓取的返回里本来就带 document_id，白拿。

    返回形如：

        ## 文档评论（未解决，共 2 条讨论）

        ### 针对原文「支付超时后订单状态」
        - 超时时间是 15 分钟还是 30 分钟？
        - 以配置为准，默认 15
    """
    if not (doc_token and doc_token.strip()) and not (url and url.strip()):
        return None

    settings = get_settings()
    timeout_sec = float(timeout if timeout is not None else settings.lark_cli_timeout_seconds)
    ident = identity or settings.lark_cli_identity

    items = await _list_all_comments(
        url, doc_token=doc_token, timeout_sec=timeout_sec, identity=ident,
    )
    if not items:
        return None

    threads = [t for t in (_render_thread(it) for it in items) if t]
    if not threads:
        logger.info("comments: %d item(s) fetched but none had usable text | url=%s", len(items), url[:80])
        return None

    n_solved = sum(1 for it in items if it.get("is_solved"))
    lines = [
        f"\n\n## 文档评论（共 {len(threads)} 条讨论，其中已确认 {n_solved} 条）",
        # 给下游 Clarifier/Generator 的读法说明：评论区的结论常常没回写正文，且一串讨论里
        # 最后一条才是定论（实测有「跳转 A」→ 讨论 →「改成 B」这种反转）。
        "> 说明：评论中的结论未必已同步到上方正文，应与正文同等对待。"
        "「已确认」表示该讨论已有定论；「讨论中」的分歧点适合作为澄清问题。"
        "同一讨论内按时间先后排列，若前后结论冲突，以最后一条为准。",
    ]
    lines.extend(threads)
    appendix = "\n".join(lines)
    logger.info(
        "comments done | threads=%d/%d appendix_chars=%d | url=%s",
        len(threads), len(items), len(appendix), url[:80],
    )
    return appendix


# ── lark-cli 调用 ─────────────────────────────────────────────────────────────


async def _list_all_comments(
    url: str, *, doc_token: str | None, timeout_sec: float, identity: str,
) -> list[dict]:
    """翻页取全部未解决评论。

    页数上限（lark_comments_max_pages）是防守性的：评论几百条的文档基本是设计评审记录，
    全塞进正文只会淹没需求本身，取前几页足够。
    """
    settings = get_settings()
    out: list[dict] = []
    page_token: str | None = None

    for page in range(max(1, settings.lark_comments_max_pages)):
        payload = await _run_list(
            url, doc_token=doc_token, page_token=page_token,
            timeout_sec=timeout_sec, identity=identity,
        )
        if payload is None:
            break

        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        items = data.get("items")
        if isinstance(items, list):
            out.extend(x for x in items if isinstance(x, dict))

        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or None
        if not page_token:
            break
        logger.info("comments: fetching page %d | url=%s", page + 2, url[:80])

    return out


async def _run_list(
    url: str, *, doc_token: str | None, page_token: str | None,
    timeout_sec: float, identity: str,
) -> dict | None:
    """跑一次 `lark-cli drive +list-comments`，返回解析后的 payload；任何失败返回 None。

    显式传 --solved-status all：CLI 默认值是 false（只给未解决），而已解决的评论往往承载
    最确定的需求结论且未必回写正文，必须一并取回。见模块 docstring。
    """
    settings = get_settings()
    cli = settings.lark_cli_path or "lark-cli"
    cmd = [cli, "drive", "+list-comments"]
    # token 路径绕开 wiki 解析（省掉 wiki:* scope）；没有 token 才退回 --url
    if doc_token and doc_token.strip():
        cmd += ["--token", doc_token.strip(), "--type", "docx"]
    else:
        cmd += ["--url", url]
    cmd += [
        "--as", identity,
        "--solved-status", "all",
        "--page-size", str(_clamp(settings.lark_comments_page_size, 1, 100)),
        "--format", "json",
    ]
    if page_token:
        cmd += ["--page-token", page_token]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning("comments: cannot spawn lark-cli (%s): %s", cli, exc)
        return None

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        logger.warning("comments: list timeout (%ss) | url=%s", timeout_sec, url[:80])
        return None

    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    # 实测：成功时 JSON 打到 stdout；失败时 stdout 为空，进度行 + {"ok":false} envelope
    # 全写到 stderr。所以两个流都得试，否则错误分类拿不到 error.type。
    payload = _try_json(stdout) or _try_json(stderr)
    if payload is None:
        if proc.returncode != 0:
            logger.warning(
                "comments: returncode=%d | url=%s | %s",
                proc.returncode, url[:80], (stderr.strip() or stdout.strip())[:200],
            )
        return None
    if payload.get("ok") is False:
        err = payload.get("error") or {}
        msg = str(err.get("message") or "未知错误")
        # 权限/授权类失败是部署常态（评论 scope 与正文抓取不是一套），提示得具体些便于排查
        if err.get("type") in ("authorization", "authentication"):
            logger.warning(
                "comments: 无权读取评论（identity=%s）——正文导入不受影响。%s",
                identity, msg,
            )
        else:
            logger.warning("comments: list failed | url=%s: %s", url[:80], msg)
        return None
    return payload


# ── 渲染 ──────────────────────────────────────────────────────────────────────


def _render_thread(item: dict) -> str:
    """把一条评论串渲染成 markdown 片段；无可用文字时返回空串。

    标题带状态标签：已解决 →「已确认」，未解决 →「讨论中」。两者都保留，靠标签让 LLM
    区分权重（已确认按结论处理，讨论中的分歧点适合转成澄清问题）。
    """
    replies = _replies(item)
    texts = [t for t in (_reply_text(r) for r in replies) if t]
    if not texts:
        return ""

    status = "已确认" if item.get("is_solved") else "讨论中"
    quote = _clean(item.get("quote") or "")
    anchor = f"针对原文「{_clip(quote, 80)}」" if quote else "全文评论"
    body = "\n".join(f"- {_clip(t, _REPLY_MAX_CHARS)}" for t in texts)
    # 回复分页截断时留个记号，免得 LLM 把半截讨论当成完整结论
    if item.get("has_more"):
        body += "\n- （该讨论还有更多回复未取回）"
    return f"\n### {anchor}（{status}）\n{body}"


def _replies(item: dict) -> list[dict]:
    """取评论串里的回复数组（reply_list.replies）。"""
    rl = item.get("reply_list")
    if isinstance(rl, dict):
        replies = rl.get("replies")
        if isinstance(replies, list):
            return [r for r in replies if isinstance(r, dict)]
    return []


def _reply_text(reply: dict) -> str:
    """从一条回复的 content.elements[] 里抽纯文本。

    elements 在 API schema 里是 untyped 数组，实测是混合类型的定长结构：每个元素三个 key
    （text_run / person / docs_link）都在，不匹配的为 null，靠 type 区分。

    - text_run：正文，直接取
    - docs_link：取 url 原文。实测评论里大量「按钮跳转：<url>」，URL 本身就是可测的
      跳转目标，换成「[链接]」占位会让整条评论失去信息量
    - person：丢弃，@某人对生成用例没有价值
    """
    content = reply.get("content")
    if not isinstance(content, dict):
        return ""
    elements = content.get("elements")
    if not isinstance(elements, list):
        return ""

    parts: list[str] = []
    for e in elements:
        if not isinstance(e, dict):
            continue
        tr = e.get("text_run")
        link = e.get("docs_link")
        if isinstance(tr, dict):
            parts.append(str(tr.get("text") or ""))
        elif isinstance(link, dict):
            url = str(link.get("url") or "").strip()
            parts.append(url or "[链接]")
    return _clean("".join(parts))


# ── helpers ──────────────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _clip(text: str, limit: int) -> str:
    s = (text or "").strip()
    if limit > 0 and len(s) > limit:
        return s[:limit].rstrip() + "…"
    return s


def _clamp(v: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


def _try_json(stdout: str) -> dict | None:
    """lark-cli 会在 JSON 前打印 `Resolving wiki node: ...` 之类的进度行，兜底从第一个 `{` 抠。"""
    s = (stdout or "").strip()
    if not s:
        return None
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        brace = s.find("{")
        if brace < 0:
            return None
        try:
            v = json.loads(s[brace:])
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            return None
