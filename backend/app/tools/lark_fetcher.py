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

logger = logging.getLogger("caseweave.lark_fetcher")

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
# markdown 正文的首行一级标题（`# 文档标题`）——lark-cli markdown 模式的标题落点。
_MD_H1_RE = re.compile(r"#[ \t]+(?P<title>[^\n]+)")

# XML 正文里图片以 <img .../> 出现。注意 lark-cli 实测输出（v1.x）与官方 skill 文档不一致：
# 文档写的是 <img token="..." url="..."/>，实际输出是 <img name="x.png" href="下载URL"
#   mime="image/png" src="媒体token"/> —— 即 token 在 src=、直链在 href=。
# 因此两种命名都兜住：token 优先 src，回退 token；mime 直接取标签属性。
_IMG_TAG_RE = re.compile(r"<img\b[^>]*?/?>", re.IGNORECASE)
_TOKEN_ATTR_RE = re.compile(r'\b(?:src|token)="([^"]+)"', re.IGNORECASE)
_MIME_ATTR_RE = re.compile(r'\bmime="([^"]+)"', re.IGNORECASE)
# 内嵌画板：XML 正文里以 <whiteboard token="wbcn..." .../> 出现（token 为 whiteboard_id）。
# 画板没有独立 file_token，只能通过 `+media-download --type whiteboard` 拿缩略图，再走视觉识别。
_WHITEBOARD_TAG_RE = re.compile(r"<whiteboard\b[^>]*?/?>", re.IGNORECASE)
# markdown 正文里残留的画板标签（实测是成对形式 `<whiteboard token="…"></whiteboard>`，
# 也兜住自闭合写法），用于替换成中文占位。
_WHITEBOARD_MD_RE = re.compile(
    r"<whiteboard\b[^>]*?(?:/>|>\s*</whiteboard\s*>)", re.IGNORECASE,
)
_WB_TOKEN_ATTR_RE = re.compile(r'\btoken="([^"]+)"', re.IGNORECASE)
# 标题标签 <h1..h6 ...>文字</h1..>；用来给每张图归类章节上下文。
_HEADING_TAG_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
# 去 XML 标签取纯文本（heading 文字里可能套 <text> 等子标签）。
_XML_TAG_STRIP_RE = re.compile(r"<[^>]+>")


@dataclass
class LarkContent:
    title: str
    raw_text: str  # markdown / 纯文本，可直接喂 LLM
    kind: LarkKind  # 实际识别到的类型


@dataclass
class ImgRef:
    """XML 正文里枚举到的一张图片：token + mime + 所在章节标题（就近的上一个 heading）。

    kind 区分素材来源：
    - "media"      普通图片素材（<img>），token 是 file_token，走 --type media 下载；
    - "whiteboard" 内嵌画板（<whiteboard>），token 是 whiteboard_id，走 --type whiteboard 下载缩略图。
    """
    token: str
    heading: str | None
    mime: str | None = None
    kind: str = "media"


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

    抓完正文后按开关做增强，结果一律 append 到正文；全程 fail-open，失败退回纯正文：
    - 内嵌画板：结构化提取（lark_whiteboard_enabled，默认开）
    - 配图：视觉模型识别（vision_enabled）
    - 未解决评论：`drive +list-comments`（lark_comments_enabled，默认关，需额外 scope）
    """
    kind = classify_lark_url(url)
    if kind == "unknown":
        raise LarkUrlInvalid(f"不是合法的飞书文档链接：{url}")
    if kind == "sheet":
        raise LarkSheetUnsupported("飞书电子表格暂不支持，请将表格导出为 xlsx 后再上传。")

    settings = get_settings()
    timeout_sec = float(timeout if timeout is not None else settings.lark_cli_timeout_seconds)

    logger.info("lark fetch | kind=%s url=%s", kind, url[:120])
    # --doc-format markdown：拿干净的 markdown 正文喂 LLM（默认是 DocxXML，不适合直接生成用例）。
    payload = await _run_fetch(url, doc_format="markdown", timeout_sec=timeout_sec)

    if payload.get("ok") is False:
        err = payload.get("error") or {}
        msg = str(err.get("message") or "未知错误")
        _classify_and_raise(msg, err)

    title, text = _extract_title_and_text(payload)
    # markdown 正文里画板会原样留下 `<whiteboard token="…"></whiteboard>` —— 对 LLM 是纯噪声
    # （token 无意义），但「这里有张图」这个信号有用，换成中文占位；画板内容走文末增强段落。
    text = _WHITEBOARD_MD_RE.sub("（此处为文档内嵌画板，内容见文末「文档配图与画板」）", text)

    # ── 配图 / 画板增强（可选）─────────────────────────────────────────────────
    # 把文档配图（需 vision）和内嵌画板（结构化提取，不需要 vision）识别成文字 append 到
    # 正文；纯图片/纯画板文档（正文为空）也能靠这段救回来。异常一律 fail-open 退回原正文。
    if settings.vision_enabled or settings.lark_whiteboard_enabled:
        try:
            enriched = await enrich_content_with_media(
                url, base_text=text, timeout=timeout_sec,
            )
            if enriched and enriched.strip():
                text = enriched
        except Exception as exc:
            logger.warning("media enrichment failed (fallback to text-only): %s", exc)

    # 空文档判定必须在评论增强之前：配图/画板能把纯图片文档「救回来」（那本就是正文的一部分），
    # 但一篇没有正文的文档不该靠评论凑数 —— 评论是对需求的批注，不是需求本身。
    if not text or not text.strip():
        raise LarkEmptyDoc("文档正文为空（可能仅含图片或图表，无法用于生成测试用例）。")

    # ── 评论增强（可选）────────────────────────────────────────────────────────
    # 评论区常藏着需求歧义的直接线索，且结论未必回写正文。fail-open：拿不到只记日志，
    # 不影响正文导入。
    if settings.lark_comments_enabled:
        try:
            from app.tools.lark_comments import fetch_document_comments

            # 带上 document_id：让评论接口跳过 wiki 链接解析，省掉 bot 身份没有的 wiki:* scope
            comments = await fetch_document_comments(
                url, doc_token=_extract_document_id(payload), timeout=timeout_sec,
            )
            if comments and comments.strip():
                text = text.rstrip() + comments
        except Exception as exc:
            logger.warning("comment enrichment failed (fallback to text-only): %s", exc)

    return LarkContent(title=title or "未命名文档", raw_text=text, kind=kind)


async def _run_fetch(url: str, *, doc_format: str, timeout_sec: float) -> dict:
    """跑一次 `lark-cli docs +fetch`，返回解析后的 payload dict。

    doc_format: markdown（正文喂 LLM）| xml（保留 <img token url> 等结构化标签）。
    仅在完全拿不到可解析 JSON 时抛 LarkFetchFailed；{"ok": false} 也照样返回给调用方判定。

    --as user/bot：抓取身份，默认 bot（应用凭证），见 settings.lark_cli_identity。
    """
    settings = get_settings()
    cli = settings.lark_cli_path or "lark-cli"
    cmd = [cli, "docs", "+fetch", "--doc", url, "--as", settings.lark_cli_identity,
           "--doc-format", doc_format, "--format", "json"]

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

    # lark-cli 出错时会输出一份 {"ok": false, "error": ...} envelope，所以不能只看 returncode。
    # 注意：实测成功时 envelope 打在 stdout，**失败时 stdout 为空、envelope 连同进度行一起写
    # stderr**。只解析 stdout 会让下面的 _classify_and_raise 永远拿不到 error.message —— 未登录
    # 之类的错误就退化成「returncode=3」+ 一坨 JSON 原文，而不是「请先 lark-cli auth login」。
    payload: dict | None = None
    for stream in (stdout, stderr):
        try:
            payload = _extract_json(stream)
            break
        except ValueError:
            continue

    if proc.returncode != 0 and payload is None:
        # 完全没拿到 JSON —— 可能是 npm 包损坏或者环境问题
        logger.error("lark-cli returned %d with no parseable JSON | stderr=%s", proc.returncode, stderr[:500])
        raise LarkFetchFailed(f"lark-cli 调用失败（returncode={proc.returncode}）：{stderr.strip()[:300] or stdout.strip()[:300]}")

    if not payload or not isinstance(payload, dict):
        raise LarkFetchFailed("lark-cli 返回内容无法解析为 JSON")

    return payload


# ── 图片增强（图片 → 文字）──────────────────────────────────────────────────────


def _enumerate_images_from_xml(xml_text: str) -> list[ImgRef]:
    """按文档顺序扫出所有 <img> 与 <whiteboard> 并给每张图归到就近的上一个 heading。

    做法：把 heading / img / whiteboard 标签统一按出现位置排序，边扫边维护 current_heading。
    画板（<whiteboard token=.../>）当作一种特殊「图片」纳入同一识别链路：下载其缩略图后走视觉识别。
    """
    events: list[tuple[int, str, str, str | None]] = []  # (pos, kind, value, mime)
    for m in _HEADING_TAG_RE.finditer(xml_text):
        head_txt = _XML_TAG_STRIP_RE.sub("", m.group(2)).strip()
        events.append((m.start(), "heading", head_txt, None))
    for m in _IMG_TAG_RE.finditer(xml_text):
        tok_m = _TOKEN_ATTR_RE.search(m.group(0))
        if tok_m:
            mime_m = _MIME_ATTR_RE.search(m.group(0))
            events.append((m.start(), "img", tok_m.group(1), mime_m.group(1) if mime_m else None))
    for m in _WHITEBOARD_TAG_RE.finditer(xml_text):
        tok_m = _WB_TOKEN_ATTR_RE.search(m.group(0))
        if tok_m:
            # 画板缩略图统一按 PNG 处理（media-download 缩略图为位图），mime 交给下载兜底
            events.append((m.start(), "whiteboard", tok_m.group(1), None))
    events.sort(key=lambda e: e[0])

    imgs: list[ImgRef] = []
    current_heading: str | None = None
    for _pos, kind, value, mime in events:
        if kind == "heading":
            current_heading = value or current_heading
        elif kind == "whiteboard":
            imgs.append(ImgRef(token=value, heading=current_heading, mime=mime, kind="whiteboard"))
        else:
            imgs.append(ImgRef(token=value, heading=current_heading, mime=mime, kind="media"))
    return imgs


async def enrich_content_with_media(url: str, *, base_text: str, timeout: float | None = None) -> str:
    """枚举文档配图与内嵌画板 → 转成文字 → 汇总 append 到 base_text。

    两条支线：
    - 画板（<whiteboard>）：先走 `whiteboard +query` 结构化提取（Mermaid 源码 / 节点+连线
      大纲，纯文本、零 LLM 成本）；拿不到再退回缩略图 + 视觉识别（需 vision_enabled）。
    - 配图（<img>）：下载 + 视觉识别，仅在 vision_enabled 时处理。

    全程 fail-open：任何一步失败就跳过（该图 / 整个增强），返回时至少不劣于 base_text。
    """
    # 延迟 import，避免未开启 vision 时也加载视觉/下载依赖
    from app.agents.image_describer import describe_image
    from app.tools.lark_media import download_lark_media, LarkMediaError
    from app.tools.lark_whiteboard import fetch_whiteboard_text

    settings = get_settings()
    timeout_sec = float(timeout if timeout is not None else settings.lark_cli_timeout_seconds)

    # 再抓一次 XML 拿结构化 <img> 标签（markdown 格式丢 token）
    try:
        xml_payload = await _run_fetch(url, doc_format="xml", timeout_sec=timeout_sec)
    except LarkFetchError as exc:
        logger.warning("media enrichment: xml fetch failed: %s", exc)
        return base_text
    if xml_payload.get("ok") is False:
        return base_text

    _title, xml_text = _extract_title_and_text(xml_payload)
    if not xml_text:
        return base_text

    imgs = _enumerate_images_from_xml(xml_text)
    if not imgs:
        logger.info("media enrichment: no <img>/<whiteboard> tags found | url=%s", url[:80])
        return base_text

    # 按开关过滤：画板要 lark_whiteboard_enabled（或 vision 走缩略图兜底）；配图只认 vision。
    def _wanted(ref: ImgRef) -> bool:
        if ref.kind == "whiteboard":
            return settings.lark_whiteboard_enabled or settings.vision_enabled
        return settings.vision_enabled

    imgs = [ref for ref in imgs if _wanted(ref)]
    if not imgs:
        return base_text

    dropped = 0
    if len(imgs) > settings.vision_max_images:
        dropped = len(imgs) - settings.vision_max_images
        imgs = imgs[: settings.vision_max_images]
    logger.info(
        "media enrichment: %d item(s) to process%s | url=%s",
        len(imgs), f"（另丢弃 {dropped} 个，超出 VISION_MAX_IMAGES）" if dropped else "", url[:80],
    )

    sem = asyncio.Semaphore(max(1, settings.vision_concurrency))

    async def _one(idx: int, ref: ImgRef) -> tuple[int, ImgRef, str]:
        async with sem:
            # 画板优先走结构化提取（拿到的是原文节点/连线，比看图更准，也不花 LLM token）
            if ref.kind == "whiteboard" and settings.lark_whiteboard_enabled:
                try:
                    text = await fetch_whiteboard_text(ref.token, timeout=timeout_sec)
                except Exception as exc:  # noqa: BLE001 — 结构化提取绝不该拖垮导入
                    logger.warning("whiteboard extract crashed token=%s: %s", ref.token[:12], exc)
                    text = None
                if text:
                    return idx, ref, text
                if not settings.vision_enabled:
                    return idx, ref, ""

            try:
                data, mime = await download_lark_media(
                    ref.token, timeout=timeout_sec, media_type=ref.kind,
                )
            except LarkMediaError as exc:
                logger.warning(
                    "media enrichment: download failed kind=%s token=%s: %s",
                    ref.kind, ref.token[:12], exc,
                )
                return idx, ref, ""
            # XML 标签自带的 mime 更可靠，优先用它；下载兜底猜的 mime 作后备
            desc = await describe_image(data, ref.mime or mime, heading=ref.heading, kind=ref.kind)
            return idx, ref, desc

    results = await asyncio.gather(
        *[_one(i, ref) for i, ref in enumerate(imgs)], return_exceptions=True,
    )

    described: list[tuple[int, ImgRef, str]] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("media enrichment: task crashed: %s", r)
            continue
        idx, ref, desc = r
        if desc and desc.strip():
            described.append((idx, ref, desc.strip()))

    if not described:
        logger.info("media enrichment: no usable descriptions produced | url=%s", url[:80])
        return base_text

    described.sort(key=lambda t: t[0])
    n_wb = sum(1 for _i, ref, _d in described if ref.kind == "whiteboard")
    n_img = len(described) - n_wb
    summary = "、".join(
        p for p in (f"配图 {n_img} 张" if n_img else "", f"画板 {n_wb} 个" if n_wb else "") if p
    )
    lines: list[str] = [f"\n\n## 文档配图与画板（自动识别，共{summary}）"]
    for n, (_idx, ref, desc) in enumerate(described, start=1):
        head = f" · 章节「{ref.heading}」" if ref.heading else ""
        label = "画板" if ref.kind == "whiteboard" else "配图"
        lines.append(f"\n### {label} {n}{head}\n{desc}")
    appendix = "\n".join(lines)

    logger.info(
        "media enrichment done | described=%d/%d appendix_chars=%d | url=%s",
        len(described), len(imgs) + dropped, len(appendix), url[:80],
    )
    # base_text 可能为空（纯图片文档）——此时增强段落就是全部正文
    return (base_text.rstrip() + appendix) if base_text and base_text.strip() else appendix.lstrip()


# ── helpers ──────────────────────────────────────────────────────────────────


def _extract_json(text: str) -> dict:
    """从 lark-cli 的一路输出（stdout 或 stderr）里抠 JSON envelope。

    lark-cli 会在 JSON 前打印 deprecation/notice/进度行（如 `Resolving wiki node: …`），
    兜底从第一个 `{` 开始解析。
    """
    s = text.strip()
    if not s:
        raise ValueError("empty output")
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


def _classify_and_raise(message: str, error: dict | None = None) -> None:
    """把 lark-cli 的错误归类成具体异常。

    优先用 envelope 里的结构化 `error.type`（authentication / authorization），文案匹配只作
    兜底 —— 实测文案匹配单独用并不可靠：`need_user_authorization` 匹不上 "unauthorized"，
    `access denied` 也匹不上 "deny"（"denied" 不含 "deny"），两类最常见的错误全落到通用分支。
    """
    etype = str((error or {}).get("type") or "").lower()
    if etype == "authentication":
        raise LarkCliNotLoggedIn("lark-cli 未登录或 token 已过期，请在容器内执行 `lark-cli auth login`。")
    if etype == "authorization":
        raise LarkPermissionDenied("没有该飞书文档的访问权限，请在飞书侧给本应用/账号开通读取权限。")

    m = message.lower()
    if "未登录" in message or "login" in m or "authorization" in m or "unauthorized" in m:
        raise LarkCliNotLoggedIn("lark-cli 未登录或 token 已过期，请在容器内执行 `lark-cli auth login`。")
    if "权限" in message or "permission" in m or "forbidden" in m or "denied" in m or "scope" in m:
        raise LarkPermissionDenied("没有该飞书文档的访问权限，请在飞书侧给本应用/账号开通读取权限。")
    if "not found" in m or "不存在" in message or "404" in m:
        raise LarkFetchFailed("文档不存在或链接已失效。")
    raise LarkFetchFailed(f"飞书抓取失败：{message}")


def _extract_document_id(payload: dict) -> str | None:
    """抠出 docx 的 document_id（成功 shape 里在 data.document.document_id）。

    用于评论接口：直接给 token 就不必让 lark-cli 去解析 wiki 链接（那需要额外 scope）。
    """
    for root in (payload.get("data"), payload):
        if not isinstance(root, dict):
            continue
        for holder in (root.get("document"), root):
            if isinstance(holder, dict):
                v = holder.get("document_id")
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None


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

    # markdown 模式下 lark-cli（实测 1.0.69）不发 <title> 标签，而是把文档标题写成首行 H1
    # （`# 某某功能`）—— 此时用首行 H1 回填标题，否则整篇会落成「未命名文档」。
    # 只认首个非空行、且正文里保留该 H1（它同时是正文的一级标题，删了反而丢结构）。
    if not title and text:
        m = _MD_H1_RE.match(text.lstrip())
        if m:
            title = m.group("title").strip()

    if not text:
        # 接到未识别 shape 时打印 keys，方便适配
        logger.warning(
            "lark-cli payload had no recognizable text field | keys=%s",
            list(payload.keys()),
        )

    return title, text
