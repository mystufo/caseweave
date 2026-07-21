"""Fetch Feishu/Lark document content via the local `lark-cli` binary.

Why subprocess instead of OpenAPI SDK: 用户已经在本机用 lark-cli 登录，
直接复用它的认证（App ID / user token）和 MCP fetch-doc 路径，不需要在
后端再维护一份 token 刷新逻辑。

支持类型:
- docx (新版文档) — `lark-cli docs +fetch --doc <URL>`
- wiki (知识库)   — 同上（lark-cli 的 v1 fetch-doc 走 MCP，能直接吃 wiki URL）
- docs (旧版文档) — 同上
- sheet           — 暂不支持，由调用方拦截后给出友好提示
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from app.config import get_settings

logger = logging.getLogger("testcraft.lark_fetcher")

LarkKind = Literal["docx", "wiki", "docs", "sheet", "unknown"]

# 允许的 URL 形式：含 feishu.cn / larksuite.com / lark.com 任一域名
# 路径段含 /docx/ /wiki/ /docs/ /sheets/ 之一，token 至少 15 字符
_LARK_URL_RE = re.compile(
    r"^https?://[\w.-]*(?:feishu|larksuite|lark)\.[\w.-]+/"
    r"(?P<kind>docx|wiki|docs|sheets?|sheet)/"
    r"(?P<token>[A-Za-z0-9]{15,})",
    re.IGNORECASE,
)

# lark-cli v2 fetch-doc 把文档标题内嵌成 `<title>…</title>` 前缀（markdown/xml 皆如此）
_TITLE_TAG_RE = re.compile(r"<title>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class LarkContent:
    title: str
    raw_text: str  # markdown / 纯文本，可直接喂 LLM
    kind: LarkKind  # 实际识别到的类型


# ── 异常族 ────────────────────────────────────────────────────────────────────


class LarkFetchError(Exception):
    """所有飞书抓取相关错误的基类。"""


class LarkUrlInvalid(LarkFetchError):
    """URL 格式不对、不是飞书域名、或不是支持的文档类型。"""


class LarkSheetUnsupported(LarkFetchError):
    """飞书电子表格暂不支持（链路复杂，留待后续迭代）。"""


class LarkCliNotInstalled(LarkFetchError):
    """容器/主机里找不到 lark-cli 可执行文件。"""


class LarkCliNotLoggedIn(LarkFetchError):
    """lark-cli 未登录，需要先 `lark-cli auth login`。"""


class LarkPermissionDenied(LarkFetchError):
    """文档存在但当前身份无权读取。"""


class LarkFetchTimeout(LarkFetchError):
    """lark-cli 调用超时。"""


class LarkEmptyDoc(LarkFetchError):
    """文档抓回来了但正文为空（多半是只含图片或图表）。"""


class LarkFetchFailed(LarkFetchError):
    """其他不可分类的失败 —— 通常是飞书侧 5xx 或文档不存在。"""


# ── URL 分类 ─────────────────────────────────────────────────────────────────


def classify_lark_url(url: str) -> LarkKind:
    """识别 URL 是哪种飞书文档；非法返回 `unknown`。"""
    if not isinstance(url, str):
        return "unknown"
    m = _LARK_URL_RE.match(url.strip())
    if not m:
        return "unknown"
    kind = m.group("kind").lower()
    if kind in ("sheets", "sheet"):
        return "sheet"
    if kind in ("docx", "wiki", "docs"):
        return kind  # type: ignore[return-value]
    return "unknown"


# ── 主函数 ────────────────────────────────────────────────────────────────────


async def fetch_lark_content(url: str, timeout: float | None = None) -> LarkContent:
    """通过 lark-cli 抓取飞书文档内容。

    抛出对应的 LarkFetchError 子类，由调用方映射成 SSE error 消息。
    """
    kind = classify_lark_url(url)
    if kind == "unknown":
        raise LarkUrlInvalid(f"不是合法的飞书文档链接：{url}")
    if kind == "sheet":
        raise LarkSheetUnsupported("飞书电子表格暂不支持，请将表格导出为 xlsx 后再上传。")

    settings = get_settings()
    cli = settings.lark_cli_path or "lark-cli"
    timeout_sec = float(timeout if timeout is not None else settings.lark_cli_timeout_seconds)

    # lark-cli v1 fetch-doc 已下线，现走 v2：能直接吃 docx/wiki/docs 三种 URL。
    # --doc-format markdown：拿干净的 markdown 正文喂 LLM（默认是 DocxXML，不适合直接生成用例）。
    # 成功响应 shape 见 `_extract_title_and_text`：正文在 data.document.content，
    # 标题以 <title>…</title> 前缀内嵌在正文里。
    cmd = [cli, "docs", "+fetch", "--doc", url, "--doc-format", "markdown", "--format", "json"]
    logger.info("lark fetch | kind=%s url=%s", kind, url[:120])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise LarkCliNotInstalled(
            f"找不到 lark-cli 可执行文件（path={cli}）：{exc}"
        ) from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise LarkFetchTimeout(f"lark-cli 抓取超时（{timeout_sec:.0f}s）") from exc

    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    # lark-cli 即使出错也会用 stdout 输出一份 {"ok": false, "error": ...}，所以不能只看 returncode
    payload: dict | None = None
    try:
        payload = _extract_json(stdout)
    except ValueError:
        payload = None

    if proc.returncode != 0 and payload is None:
        # 完全没拿到 JSON —— 可能是 npm 包损坏或者环境问题
        logger.error("lark-cli returned %d with no parseable JSON | stderr=%s", proc.returncode, stderr[:500])
        raise LarkFetchFailed(f"lark-cli 调用失败（returncode={proc.returncode}）：{stderr.strip()[:300] or stdout.strip()[:300]}")

    if not payload or not isinstance(payload, dict):
        raise LarkFetchFailed("lark-cli 返回内容无法解析为 JSON")

    if payload.get("ok") is False:
        err = payload.get("error") or {}
        msg = str(err.get("message") or "未知错误")
        _classify_and_raise(msg)

    title, text = _extract_title_and_text(payload)
    if not text or not text.strip():
        raise LarkEmptyDoc("文档正文为空（可能仅含图片或图表，无法用于生成测试用例）。")

    return LarkContent(title=title or "未命名文档", raw_text=text, kind=kind)


# ── helpers ──────────────────────────────────────────────────────────────────


def _extract_json(stdout: str) -> dict:
    """lark-cli 偶尔会在 JSON 前打印 deprecation/notice 行，兜底从第一个 `{` 抠出来。"""
    s = stdout.strip()
    if not s:
        raise ValueError("empty stdout")
    # 直接尝试整体解析
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 找第一个 `{` 开始解析（lark-cli 输出始终是单个 JSON 对象）
    brace = s.find("{")
    if brace < 0:
        raise ValueError("no JSON object in stdout")
    try:
        return json.loads(s[brace:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"json decode failed: {exc}") from exc


def _classify_and_raise(message: str) -> None:
    """把 lark-cli 的错误消息归类成具体异常 —— 文案匹配，宽松一点。"""
    m = message.lower()
    if "未登录" in message or "login" in m or "unauthorized" in m or "auth" in m and "expir" in m:
        raise LarkCliNotLoggedIn("lark-cli 未登录或 token 已过期，请在容器内执行 `lark-cli auth login`。")
    if "权限" in message or "permission" in m or "forbidden" in m or "access" in m and "deny" in m:
        raise LarkPermissionDenied("没有该飞书文档的访问权限，请在飞书侧给本应用/账号开通读取权限。")
    if "not found" in m or "不存在" in message or "404" in m:
        raise LarkFetchFailed("文档不存在或链接已失效。")
    raise LarkFetchFailed(f"飞书抓取失败：{message}")


def _extract_title_and_text(payload: dict) -> tuple[str, str]:
    """从 lark-cli 的成功响应里抠 title 和正文。

    lark-cli v2 fetch-doc 的成功 shape：
        {"ok": true, "data": {"document": {"content": "<title>标题</title>\\n\\n# 正文…",
                                            "document_id": "...", "revision_id": 12}}}

    正文以 markdown 输出（backend 传了 --doc-format markdown），标题被内嵌成
    `<title>…</title>` 前缀，需要单独抠出来并从正文里剥掉。

    仍保留对历史 shape（result / 顶层 markdown 字段 / MCP content list）的宽容兜底。
    """
    # data.document 是 v2 的正文所在；连同历史 shape 一起纳入候选根
    candidates_root: list[dict] = [payload]
    for key in ("data", "result", "doc", "document"):
        v = payload.get(key)
        if isinstance(v, dict):
            candidates_root.append(v)
            # 再下潜一层（v2 是 data.document）
            for subkey in ("document", "doc", "result"):
                sv = v.get(subkey)
                if isinstance(sv, dict):
                    candidates_root.append(sv)

    # 提 title
    title = ""
    for d in candidates_root:
        for k in ("title", "name", "doc_title"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                title = v.strip()
                break
        if title:
            break

    # 提正文
    text = ""
    for d in candidates_root:
        for k in ("content", "markdown", "md", "text", "raw_text", "body", "plain_text"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                text = v
                break
        if text:
            break
        # MCP 风格：content 是 list[{type:"text", text:"..."}]
        content = d.get("content")
        if isinstance(content, list):
            parts = []
            for it in content:
                if isinstance(it, dict):
                    t = it.get("text") or it.get("value")
                    if isinstance(t, str):
                        parts.append(t)
                elif isinstance(it, str):
                    parts.append(it)
            if parts:
                text = "\n\n".join(parts).strip()
                break
        elif isinstance(content, str) and content.strip():
            text = content
            break

    # 剥掉内嵌的 <title>…</title> 前缀；若响应里没拿到显式 title 就用它回填
    if text:
        m = _TITLE_TAG_RE.match(text.lstrip())
        if m:
            embedded = m.group("title").strip()
            if not title and embedded:
                title = embedded
            text = text.lstrip()[m.end():].lstrip()

    if not text:
        # 接到未识别 shape 时打印 keys，方便适配
        logger.warning(
            "lark-cli payload had no recognizable text field | keys=%s",
            list(payload.keys()),
        )

    return title, text
