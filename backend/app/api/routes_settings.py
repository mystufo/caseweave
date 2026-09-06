"""管理员「系统设置」页：在网页上覆盖 .env 的 LLM / 视觉配置。

三个接口：读快照、保存（落库 + 热更新）、测试连接（拿表单值试发一次极小请求，不动全局）。
哪些键可改、怎么校验、怎么加密都在 app/settings_store.py，这里只做 HTTP 壳。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct
import time
import zlib
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store
from app.agents.llm_factory import make_chat_model
from app.auth import require_admin
from app.database import get_db
from app.models.user import User

logger = logging.getLogger("caseweave.settings")
router = APIRouter()


class SettingsUpdate(BaseModel):
    # 密钥字段传空串 = 不修改；其余字段按类型校验
    values: dict[str, Any] = Field(default_factory=dict)
    # 删掉这些键的页面覆盖，恢复为 .env / 默认值
    reset: list[str] = Field(default_factory=list)


class SettingsTest(BaseModel):
    group: Literal["llm", "vision"]
    # 表单当前值（可只传一部分，缺的用当前生效值补）；密钥留空 = 用已保存的
    values: dict[str, Any] = Field(default_factory=dict)


@router.get("/settings")
async def get_system_settings(_admin: User = Depends(require_admin)):
    return settings_store.snapshot()


@router.put("/settings")
async def put_system_settings(
    body: SettingsUpdate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    values, errors = settings_store.validate_updates(body.values)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    try:
        await settings_store.save_overrides(db, values, body.reset, admin.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"errors": {"_": str(exc)}})
    return settings_store.snapshot()


def _solid_png(size: int = 32, rgb: tuple[int, int, int] = (220, 30, 30)) -> bytes:
    """纯色 PNG，给视觉模型做「能不能看图」的最小探针。手写编码免得引 PIL。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * size
    raw = row * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


_PROBE_PNG_B64 = base64.b64encode(_solid_png()).decode("ascii")


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for it in content:
            if isinstance(it, dict) and isinstance(it.get("text"), str):
                parts.append(it["text"])
            elif isinstance(it, str):
                parts.append(it)
        return "\n".join(parts)
    return str(content)


@router.post("/settings/test")
async def test_system_settings(body: SettingsTest, _admin: User = Depends(require_admin)):
    """用表单里的值发一次最小请求。不记 token、不过闸门（管理员专用、开销极小）。"""
    values, errors = settings_store.validate_updates(body.values)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    cfg = settings_store.effective(body.group, values)

    if body.group == "vision":
        llm_cfg = settings_store.effective("llm", {})
        provider = cfg["vision_provider"] or llm_cfg["llm_provider"]
        model = cfg["vision_model"] or llm_cfg["llm_model"]
        api_key = cfg["vision_api_key"] or llm_cfg["llm_api_key"]
        base_url = cfg["vision_base_url"] or llm_cfg["llm_base_url"]
        timeout, retries, stream_usage = (
            llm_cfg["llm_timeout_seconds"], 0, llm_cfg["llm_stream_usage"],
        )
        message = HumanMessage(content=[
            {"type": "text", "text": "这张图是什么颜色？只回答一个词。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PROBE_PNG_B64}"}},
        ])
        # 思考型模型的 max_tokens 含 reasoning，给太小会只剩空正文
        max_tokens = 512
    else:
        provider, model = cfg["llm_provider"], cfg["llm_model"]
        api_key, base_url = cfg["llm_api_key"], cfg["llm_base_url"]
        timeout, retries, stream_usage = (
            cfg["llm_timeout_seconds"], 0, cfg["llm_stream_usage"],
        )
        message = HumanMessage(content="回复「OK」两个字母即可。")
        max_tokens = 512

    if not api_key:
        return {"ok": False, "model": model, "error": "API Key 为空"}

    start = time.perf_counter()
    try:
        llm = make_chat_model(
            provider=provider, model=model, api_key=api_key, base_url=base_url,
            max_tokens=max_tokens, temperature=0, timeout=timeout, max_retries=retries,
            stream_usage=stream_usage, record_usage=False,
        )
        # 探针不该比正常调用等更久；加 5s 兜底 SDK 自己的 timeout 没生效的情况
        resp = await asyncio.wait_for(llm.ainvoke([message]), timeout=float(timeout) + 5)
    except Exception as exc:  # noqa: BLE001 — 任何失败都要原样带给管理员看
        elapsed = (time.perf_counter() - start) * 1000
        logger.info("settings test failed | group=%s model=%s: %s", body.group, model, exc)
        err = str(exc) or type(exc).__name__
        if isinstance(exc, asyncio.TimeoutError):
            err = f"超时（{timeout:g}s 内无响应）"
        return {"ok": False, "model": model, "latency_ms": round(elapsed), "error": err[:500]}

    elapsed = (time.perf_counter() - start) * 1000
    reply = _flatten(resp.content).strip()
    usage = getattr(resp, "usage_metadata", None) or {}
    return {
        "ok": True,
        "model": model,
        "latency_ms": round(elapsed),
        "reply": reply[:200],
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    }
