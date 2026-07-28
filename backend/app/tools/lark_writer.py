"""Write content to Feishu/Lark via the local `lark-cli` binary.

与 lark_fetcher.py 对称：fetcher 负责读飞书文档，writer 负责写。
同样复用本机已登录的 lark-cli 认证（不在后端维护 token）。

当前能力：
- create_lark_doc — 用 `lark-cli docs +create` 把一段 Markdown 创建成一篇新的飞书云文档，
  返回文档 URL / token / 标题。测试脑图（Markdown 大纲）即通过它写入飞书。

异常复用 lark_fetcher 的异常族，让路由层的错误映射保持单一来源。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.config import get_settings
from app.tools.lark_fetcher import (
    LarkFetchError,
    LarkCliNotInstalled,
    LarkCliNotLoggedIn,
    LarkPermissionDenied,
    LarkFetchTimeout,
    LarkFetchFailed,
    _extract_json,  # 复用 fetcher 的宽容 JSON 提取（lark-cli 偶尔在 JSON 前打 notice 行）
)

logger = logging.getLogger("caseweave.lark_writer")


@dataclass
class LarkDoc:
    url: str
    token: str
    title: str


async def create_lark_doc(title: str, markdown: str, timeout: float | None = None) -> LarkDoc:
    """通过 lark-cli 把 Markdown 创建成一篇新的飞书云文档。

    抛出 lark_fetcher 的异常族子类，由调用方映射成 HTTP 错误。
    """
    if not markdown or not markdown.strip():
        raise LarkFetchFailed("脑图内容为空，无法创建飞书文档。")

    settings = get_settings()
    cli = settings.lark_cli_path or "lark-cli"
    timeout_sec = float(timeout if timeout is not None else settings.lark_cli_timeout_seconds)

    # --content - 从 stdin 读正文（已用 dry-run 验证支持），避免超长命令行 / 转义问题。
    cmd = [cli, "docs", "+create", "--doc-format", "markdown",
           "--title", title, "--content", "-", "--format", "json"]
    logger.info("lark create doc | title=%s md_chars=%d", title[:80], len(markdown))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise LarkCliNotInstalled(
            f"找不到 lark-cli 可执行文件（path={cli}）：{exc}"
        ) from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=markdown.encode("utf-8")), timeout=timeout_sec
        )
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise LarkFetchTimeout(f"lark-cli 创建文档超时（{timeout_sec:.0f}s）") from exc

    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    payload: dict | None = None
    try:
        payload = _extract_json(stdout)
    except ValueError:
        payload = None

    if proc.returncode != 0 and payload is None:
        logger.error(
            "lark-cli create returned %d with no parseable JSON | stderr=%s",
            proc.returncode, stderr[:500],
        )
        raise LarkFetchFailed(
            f"lark-cli 创建文档失败（returncode={proc.returncode}）："
            f"{stderr.strip()[:300] or stdout.strip()[:300]}"
        )

    if not payload or not isinstance(payload, dict):
        raise LarkFetchFailed("lark-cli 返回内容无法解析为 JSON")

    if payload.get("ok") is False:
        err = payload.get("error") or {}
        msg = str(err.get("message") or "未知错误")
        _classify_and_raise(msg)

    doc_url, doc_token = _extract_doc_url_and_token(payload)
    if not doc_token and not doc_url:
        logger.warning(
            "lark-cli create payload had no document_id/url | keys=%s",
            list(payload.keys()),
        )
        raise LarkFetchFailed("飞书文档已发起创建，但未能从返回中解析出文档链接。")

    # 只拿到 token 没拿到 url → 兜底拼 docx 链接（域名从 settings 推断，默认 feishu.cn）
    if not doc_url and doc_token:
        domain = _guess_domain(settings)
        doc_url = f"https://{domain}/docx/{doc_token}"

    return LarkDoc(url=doc_url, token=doc_token, title=title)


# ── helpers ──────────────────────────────────────────────────────────────────


def _classify_and_raise(message: str) -> None:
    """把 lark-cli 的错误消息归类成具体异常（与 fetcher 的分类口径一致）。"""
    m = message.lower()
    if "未登录" in message or "login" in m or "unauthorized" in m or ("auth" in m and "expir" in m):
        raise LarkCliNotLoggedIn("lark-cli 未登录或 token 已过期，请执行 `lark-cli auth login`。")
    if "权限" in message or "permission" in m or "forbidden" in m or ("access" in m and "deny" in m):
        raise LarkPermissionDenied("没有创建飞书文档的权限，请在飞书侧给本应用/账号开通权限。")
    raise LarkFetchFailed(f"飞书创建文档失败：{message}")


def _extract_doc_url_and_token(payload: dict) -> tuple[str, str]:
    """从 lark-cli 成功响应里抠 document url 和 token。

    已知 shape（docs +create）：
        {"ok": true, "data": {"document": {"document_id": "...", "url": "https://.../docx/..."}}}
    也可能被 CLI 拍平成 {"data": {"document_id": ..., "url": ...}} 或直接顶层。
    用宽容策略：递归收集候选 dict，逐个找 url / document_id。
    """
    # 收集所有可能承载字段的 dict（顶层 + data / result / document 嵌套）
    candidates: list[dict] = []

    def _walk(d: object, depth: int = 0) -> None:
        if depth > 4 or not isinstance(d, dict):
            return
        candidates.append(d)
        for key in ("data", "result", "document", "doc"):
            v = d.get(key)
            if isinstance(v, dict):
                _walk(v, depth + 1)

    _walk(payload)

    url = ""
    token = ""
    for d in candidates:
        if not url:
            v = d.get("url")
            if isinstance(v, str) and v.strip():
                url = v.strip()
        if not token:
            for k in ("document_id", "obj_token", "token", "doc_token"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    token = v.strip()
                    break
        if url and token:
            break
    return url, token


def _guess_domain(settings) -> str:
    """兜底域名：默认 feishu.cn。若将来 settings 增配可在此扩展。"""
    return "feishu.cn"


__all__ = [
    "LarkDoc",
    "create_lark_doc",
    # re-export 便于路由层从单一模块 import 错误类型
    "LarkFetchError",
    "LarkCliNotInstalled",
    "LarkCliNotLoggedIn",
    "LarkPermissionDenied",
    "LarkFetchTimeout",
    "LarkFetchFailed",
]
