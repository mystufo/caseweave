"""网页端可改的运行时配置：DB 覆盖 .env，热更新内存里的 Settings 对象。

优先级：system_settings 表 > .env > 代码默认值。

为什么只放白名单里的键（当前是 LLM / 视觉两组）：
- 启动引导类（DATABASE_URL / JWT_SECRET / CORS / LOG_FILE）在连上 DB 之前就得用，逻辑上无法从 DB 读；
- ADMIN_EMAILS 放到管理员页面等于自己给自己发权限；
- EMBEDDING_IMAGE / HF_ENDPOINT / DB_HOST_PORT 之类只有 docker-compose 读，后端进程根本看不见；
- 并发闸门（llm_max_concurrency…）在 limits.py 导入时就建好了信号量，改了要重启，暂不开放；
- embedding_dim 变了 pgvector 列要重建，同样不适合热改。

热更新能成立的前提：全站只有一个 Settings 实例（config.get_settings 是 lru_cache），且各处都是
调用时才读 settings.xxx（llm_factory 每次构造客户端时读）。所以 setattr 一次全局生效，
不需要重启。单进程 uvicorn 下这就是真·全局；开了 --workers 各进程要各自 load 一次（启动时会）。

密钥（api_key）落库前用 Fernet 对称加密，密钥由 JWT_SECRET 派生。JWT_SECRET 未配置时它是
进程内随机值 → 重启后解不开，此时静默回退到 .env 的值并写 warning。页面上也会提示。
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.system_setting import SystemSetting

logger = logging.getLogger("caseweave.settings_store")
settings = get_settings()

PROVIDER_OPTIONS = (("anthropic", "Anthropic 官方协议"), ("openai", "OpenAI 兼容协议"))


@dataclass(frozen=True)
class FieldSpec:
    key: str
    group: str
    label: str
    help: str = ""
    kind: str = "str"                 # str | int | float | bool | select
    secret: bool = False              # 加密落库、响应里只回掩码
    nullable: bool = False            # Optional[str]：空串即 None
    options: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # select 用 (value, label)
    min: float | None = None
    max: float | None = None
    placeholder: str = ""


GROUPS: dict[str, dict[str, str]] = {
    "llm": {
        "label": "主模型（LLM）",
        "desc": "澄清、生成、知识抽取等所有文本调用共用这一套凭证。保存后立即生效，无需重启。",
    },
    "vision": {
        "label": "视觉识别",
        "desc": "把飞书文档里的内嵌图片识别成文字。留空的项逐项回退到主模型对应配置。",
    },
}

EDITABLE: tuple[FieldSpec, ...] = (
    # ── LLM ──
    FieldSpec("llm_provider", "llm", "协议", "anthropic=官方 API；openai=火山方舟/DeepSeek/百炼/OpenRouter 等兼容网关",
              kind="select", options=PROVIDER_OPTIONS),
    FieldSpec("llm_model", "llm", "模型", "模型名或接入点 ID，如 claude-opus-4-6 / deepseek-chat / ep-2024xxx",
              placeholder="claude-opus-4-6"),
    FieldSpec("llm_api_key", "llm", "API Key", "留空表示不修改；已保存的值只显示末 4 位", secret=True),
    FieldSpec("llm_base_url", "llm", "Base URL", "留空用各协议官方默认地址；兼容网关必填，如 https://api.deepseek.com/v1",
              nullable=True, placeholder="https://..."),
    FieldSpec("llm_timeout_seconds", "llm", "单次超时（秒）", "思考型模型跑长文档慢，正常调用被打断就调大",
              kind="float", min=5, max=3600),
    FieldSpec("llm_max_retries", "llm", "失败重试次数", "总耗时上限 ≈ 超时 × (1 + 重试)",
              kind="int", min=0, max=10),
    FieldSpec("llm_stream_usage", "llm", "流式调用索要 usage", "OpenAI 兼容协议专用。个别网关不认 stream_options 会直接报错，关掉它——代价是流式调用的 token 统计不到",
              kind="bool"),
    FieldSpec("generator_max_tokens", "llm", "生成用例 max_tokens", "输出侧上限。几十条用例的 JSON 最易被截断；PRD+脑图+多轮澄清时可到 24576/32768",
              kind="int", min=1024, max=200000),
    FieldSpec("clarifier_max_tokens", "llm", "澄清问题 max_tokens", "JSON 问题列表，8192 通常够",
              kind="int", min=1024, max=200000),
    FieldSpec("knowledge_max_tokens", "llm", "知识抽取 max_tokens", "与文档体量相关，8192 一般够。思考模型的 max_tokens 包含 reasoning，别往下调",
              kind="int", min=1024, max=200000),
    FieldSpec("doc_max_chars", "llm", "文档输入字符上限", "输入侧上限，超过则保留开头 70% + 结尾。30000 字中文 ≈ 20000 token",
              kind="int", min=1000, max=2000000),
    # ── Vision ──
    FieldSpec("vision_enabled", "vision", "启用图片识别", "关闭时飞书文档里的图片直接丢弃（画板仍走结构化提取）",
              kind="bool"),
    FieldSpec("vision_provider", "vision", "协议", "留空跟随主模型",
              kind="select", options=(("", "跟随主模型"),) + PROVIDER_OPTIONS),
    FieldSpec("vision_model", "vision", "模型", "留空跟随主模型；主模型不支持多模态时必须单独填，如 doubao-1.5-vision-pro",
              placeholder="跟随主模型"),
    FieldSpec("vision_api_key", "vision", "API Key", "留空跟随主模型", secret=True),
    FieldSpec("vision_base_url", "vision", "Base URL", "留空跟随主模型", nullable=True, placeholder="跟随主模型"),
    FieldSpec("vision_max_tokens", "vision", "单图描述 max_tokens", "够写一段结构化描述即可",
              kind="int", min=128, max=32768),
    FieldSpec("vision_max_images", "vision", "单文档识别张数上限", "控成本/耗时；超出丢弃并记日志",
              kind="int", min=0, max=500),
    FieldSpec("vision_concurrency", "vision", "识别并发数", "视觉接口通常限流，别开太大",
              kind="int", min=1, max=20),
)

SPECS: dict[str, FieldSpec] = {f.key: f for f in EDITABLE}

# .env / 默认值给出的基线，用于「恢复」与显示来源。在任何 DB 覆盖发生前抓一次即可。
_baseline: dict[str, Any] = {f.key: getattr(settings, f.key) for f in EDITABLE}
_source: dict[str, str] = {}


def _default_of(key: str) -> Any:
    return Settings.model_fields[key].default


def _baseline_source(key: str) -> str:
    return "env" if _baseline[key] != _default_of(key) else "default"


for _f in EDITABLE:
    _source[_f.key] = _baseline_source(_f.key)


# ── 加解密 ──────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    digest = hashlib.sha256(f"caseweave-settings:{settings.jwt_secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def mask(value: Any) -> str:
    s = str(value or "")
    if not s:
        return ""
    return f"••••{s[-4:]}" if len(s) > 8 else "••••"


# ── 类型转换 / 校验 ────────────────────────────────────────────────────────────

def coerce(spec: FieldSpec, raw: Any) -> Any:
    """把请求/DB 里的值转成 Settings 字段类型；不合法抛 ValueError（带中文原因）。"""
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise ValueError("需要 true/false")
    if spec.kind in ("int", "float"):
        try:
            v = int(str(raw).strip()) if spec.kind == "int" else float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError("需要整数" if spec.kind == "int" else "需要数字")
        if spec.min is not None and v < spec.min:
            raise ValueError(f"不能小于 {spec.min:g}")
        if spec.max is not None and v > spec.max:
            raise ValueError(f"不能大于 {spec.max:g}")
        return v
    s = "" if raw is None else str(raw).strip()
    if spec.kind == "select":
        allowed = {o[0] for o in spec.options}
        if s not in allowed:
            raise ValueError(f"只能是 {', '.join(o or '(空)' for o in allowed)}")
        return s
    if spec.nullable:
        return s or None
    if spec.key == "llm_model" and not s:
        raise ValueError("不能为空")
    return s


def serialize(spec: FieldSpec, value: Any) -> str:
    if spec.kind == "bool":
        return "true" if value else "false"
    return "" if value is None else str(value)


def display(spec: FieldSpec, value: Any) -> Any:
    """给前端看的值：密钥只回掩码，其余原样（None → ""）。"""
    if spec.secret:
        return mask(value)
    return "" if value is None else value


# ── 读写 ──────────────────────────────────────────────────────────────────────

async def load_overrides(db: AsyncSession) -> int:
    """启动时把 DB 覆盖项灌进 settings。返回生效条数。"""
    rows = (await db.execute(select(SystemSetting))).scalars().all()
    applied = 0
    for row in rows:
        spec = SPECS.get(row.key)
        if spec is None:
            logger.info("system_settings 里有未知键 %s，忽略", row.key)
            continue
        raw = row.value
        if row.is_secret:
            try:
                raw = decrypt(raw)
            except InvalidToken:
                logger.warning(
                    "system_settings.%s 解密失败（JWT_SECRET 变了或未配置），回退到 .env 值", row.key,
                )
                continue
        try:
            value = coerce(spec, raw)
        except ValueError as exc:
            logger.warning("system_settings.%s 值非法（%s），回退到 .env 值", row.key, exc)
            continue
        setattr(settings, spec.key, value)
        _source[spec.key] = "db"
        applied += 1
    return applied


def validate_updates(values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """把请求体里的值逐项转换；返回 (合法值, 逐键错误)。密钥留空视为「不修改」，直接剔除。"""
    ok: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, raw in values.items():
        spec = SPECS.get(key)
        if spec is None:
            errors[key] = "不允许在页面修改"
            continue
        if spec.secret and not str(raw or "").strip():
            continue
        try:
            ok[key] = coerce(spec, raw)
        except ValueError as exc:
            errors[key] = str(exc)
    return ok, errors


async def save_overrides(
    db: AsyncSession, values: dict[str, Any], reset: list[str], user_id: int | None,
) -> None:
    """落库 + 热更新。values 已经过 validate_updates。reset 里的键删掉覆盖、恢复基线。"""
    for key in reset:
        if key not in SPECS:
            raise ValueError(f"{key}: 不允许在页面修改")
    for key, value in values.items():
        spec = SPECS[key]
        stored = serialize(spec, value)
        if spec.secret:
            stored = encrypt(stored)
        stmt = pg_insert(SystemSetting).values(
            key=key, value=stored, is_secret=spec.secret, updated_by=user_id,
        ).on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": stored, "is_secret": spec.secret, "updated_by": user_id},
        )
        await db.execute(stmt)
    if reset:
        await db.execute(delete(SystemSetting).where(SystemSetting.key.in_(reset)))
    await db.commit()

    for key, value in values.items():
        setattr(settings, key, value)
        _source[key] = "db"
    for key in reset:
        if key in values:
            continue
        setattr(settings, key, _baseline[key])
        _source[key] = _baseline_source(key)
    logger.info(
        "系统设置已更新 | user=%s set=%s reset=%s",
        user_id, sorted(values), sorted(reset),
    )


def snapshot() -> dict[str, Any]:
    groups = []
    for gkey, meta in GROUPS.items():
        fields = []
        for spec in EDITABLE:
            if spec.group != gkey:
                continue
            fields.append({
                "key": spec.key,
                "label": spec.label,
                "help": spec.help,
                "kind": spec.kind,
                "secret": spec.secret,
                "nullable": spec.nullable,
                "options": [{"value": v, "label": lbl} for v, lbl in spec.options],
                "min": spec.min,
                "max": spec.max,
                "placeholder": spec.placeholder,
                "value": display(spec, getattr(settings, spec.key)),
                "source": _source[spec.key],
                "baseline": display(spec, _baseline[spec.key]),
                "baseline_source": _baseline_source(spec.key),
            })
        groups.append({"key": gkey, "label": meta["label"], "desc": meta["desc"], "fields": fields})
    return {
        "groups": groups,
        # 密钥用 JWT_SECRET 派生的 key 加密；它是临时值时，页面保存的密钥重启后解不开
        "secrets_volatile": settings.jwt_secret_is_ephemeral,
    }


def effective(group: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """测试连接用：以当前生效值为底，叠上表单里的（已校验）值。"""
    out = {f.key: getattr(settings, f.key) for f in EDITABLE if f.group == group}
    for k, v in overrides.items():
        if k in out:
            out[k] = v
    return out
