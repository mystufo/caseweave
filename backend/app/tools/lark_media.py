"""Download embedded media (images) from a Feishu/Lark document via `lark-cli`.

复用 lark_fetcher 的子进程脚手架思路：调 `lark-cli docs +media-download` 把某个
media token 下载到临时文件，读回字节 + 猜 mime，用完删临时文件。

只服务于「图片 → 文字」增强链路（见 lark_fetcher.enrich_content_with_images），
所有失败都抛轻量的 LarkMediaError，由调用方 fail-open 吞掉、跳过该图。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile

from app.config import get_settings

logger = logging.getLogger("caseweave.lark_media")


class LarkMediaError(Exception):
    """下载文档 media 失败（token 失效 / 无权限 / lark-cli 异常 / 超时等）。"""


# 常见图片扩展名 → mime；视觉接口对 data URL 的 mime 较敏感，映射一份兜底。
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# 文件头魔数兜底（lark-cli 存下来的文件有时无扩展名）。
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # 粗略：RIFF....WEBP，够用
    (b"BM", "image/bmp"),
]


def _guess_mime(path: str, data: bytes) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    for sig, mime in _MAGIC:
        if data.startswith(sig):
            return mime
    return "image/png"  # 兜底：多数视觉接口能容忍


async def download_lark_media(
    token: str, *, timeout: float | None = None, identity: str | None = None,
) -> tuple[bytes, str]:
    """下载单个 media token，返回 (图片字节, mime)。

    走 `lark-cli docs +media-download --token <token> --output <相对名> --as <identity>`
    （cwd 设到临时目录），复用本机 lark-cli 登录态。identity 留空则用 settings.lark_cli_identity。
    失败抛 LarkMediaError。
    """
    if not token or not token.strip():
        raise LarkMediaError("空 media token")

    settings = get_settings()
    cli = settings.lark_cli_path or "lark-cli"
    timeout_sec = float(timeout if timeout is not None else settings.lark_cli_timeout_seconds)
    ident = identity or settings.lark_cli_identity

    # lark-cli 的 --output 只接受**相对路径**（绝对路径会被判为 "unsafe output path"）。
    # 因此建一个临时目录，把子进程 cwd 设到该目录、--output 用相对文件名，再读回。
    tmp_dir = tempfile.mkdtemp(prefix="lark_media_")
    rel_name = "media.bin"
    tmp_path = os.path.join(tmp_dir, rel_name)
    try:
        cmd = [
            cli, "docs", "+media-download",
            "--token", token,
            "--output", rel_name,
            "--overwrite",
            "--as", ident,
            "--format", "json",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmp_dir,
            )
        except FileNotFoundError as exc:
            raise LarkMediaError(f"找不到 lark-cli（path={cli}）：{exc}") from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise LarkMediaError(f"下载 media 超时（{timeout_sec:.0f}s） token={token[:12]}") from exc

        stderr = (stderr_b or b"").decode("utf-8", errors="replace")

        # lark-cli 出错时也可能用 stdout 输出 {"ok": false, ...}；先看有没有把文件写出来。
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            # 尝试解析 stdout 拿更具体的错误信息
            payload = _try_json((stdout_b or b"").decode("utf-8", errors="replace"))
            if payload and payload.get("ok") is False:
                err = (payload.get("error") or {}).get("message") or "未知错误"
                raise LarkMediaError(f"下载 media 失败：{err}")
            raise LarkMediaError(
                f"下载 media 失败（returncode={proc.returncode}）："
                f"{stderr.strip()[:200] or 'lark-cli 未写出文件'}"
            )

        with open(tmp_path, "rb") as f:
            data = f.read()
        if not data:
            raise LarkMediaError("下载的 media 文件为空")

        mime = _guess_mime(tmp_path, data)
        return data, mime
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass


def _try_json(stdout: str) -> dict | None:
    s = (stdout or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        brace = s.find("{")
        if brace < 0:
            return None
        try:
            return json.loads(s[brace:])
        except json.JSONDecodeError:
            return None
