"""调试用：把每次 LLM 调用的完整 prompt 写到磁盘。

打开方式（任选其一）：
  1. backend `.env` 里加：`PROMPT_DUMP_DIR=~/tmp/testcraft_prompts`
  2. 启动前 export 环境变量：`export PROMPT_DUMP_DIR=/tmp/testcraft_prompts`
不设置时本模块的所有函数都是 no-op，零开销。

支持 `~` 展开和绝对路径；相对路径以后端进程 CWD 为基准（不建议）。

每次调用产生一个文件，命名形如：
  20260603-201530_472_0001_generator.txt
内含三段：元信息（agent 名、长度、调用方传的上下文）/ SYSTEM / USER /（最后追加）RESPONSE。
"""
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("testcraft.prompt_dump")

_LOCK = threading.Lock()
_SEQ = 0


def _resolve_dir_setting() -> str | None:
    """优先 .env / Settings；环境变量作为兜底（避免循环依赖时也能用）。"""
    try:
        from app.config import get_settings  # 延迟导入，避免本模块被 config 导入时炸
        val = (get_settings().prompt_dump_dir or "").strip()
        if val:
            return val
    except Exception:
        pass
    return os.environ.get("PROMPT_DUMP_DIR") or None


def _enabled_dir() -> Path | None:
    raw = _resolve_dir_setting()
    if not raw:
        return None
    p = Path(raw).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("PROMPT_DUMP_DIR=%s 不可写：%s", raw, exc)
        return None
    return p


def dump_prompt(
    *,
    agent: str,
    system: str,
    user: str,
    extra: dict[str, Any] | None = None,
) -> Path | None:
    """写一份 prompt dump；环境变量没开就 no-op。返回写入的文件路径（或 None）。"""
    target = _enabled_dir()
    if target is None:
        return None

    global _SEQ
    with _LOCK:
        _SEQ += 1
        seq = _SEQ
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    millis = int((time.time() % 1) * 1000)
    fname = f"{ts}_{millis:03d}_{seq:04d}_{agent}.txt"
    path = target / fname

    parts: list[str] = []
    parts.append(f"# agent: {agent}")
    parts.append(f"# at: {datetime.now().isoformat(timespec='seconds')}")
    parts.append(f"# system_chars: {len(system or '')}")
    parts.append(f"# user_chars: {len(user or '')}")
    if extra:
        for k, v in extra.items():
            parts.append(f"# {k}: {v}")
    parts.append("")
    parts.append("=" * 60)
    parts.append("SYSTEM")
    parts.append("=" * 60)
    parts.append(system or "")
    parts.append("")
    parts.append("=" * 60)
    parts.append("USER")
    parts.append("=" * 60)
    parts.append(user or "")
    parts.append("")
    try:
        path.write_text("\n".join(parts), encoding="utf-8")
        logger.info("Dumped %s prompt to %s (system=%d, user=%d chars)",
                    agent, path, len(system or ""), len(user or ""))
        return path
    except Exception as exc:
        logger.warning("写 prompt dump 失败：%s", exc)
        return None


def dump_response(path: Path | None, response_text: str, *, finish_reason: str | None = None) -> None:
    """在 dump_prompt 返回的文件后面追加 RESPONSE 段。path 为 None（未启用）则 no-op。"""
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n")
            f.write("=" * 60 + "\n")
            f.write("RESPONSE")
            if finish_reason:
                f.write(f" (finish_reason={finish_reason})")
            f.write("\n" + "=" * 60 + "\n")
            f.write(response_text or "")
            f.write("\n")
    except Exception as exc:
        logger.warning("追加 RESPONSE 失败：%s", exc)
