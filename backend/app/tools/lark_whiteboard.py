"""Extract a Feishu/Lark whiteboard's content as **text** via `lark-cli whiteboard +query`.

为什么不直接下缩略图走视觉识别：画板（流程图/状态图/泳道图/思维导图）承载的是节点文字 +
连线走向，`whiteboard +query` 能直接把这些结构原样取回来 —— 比让视觉模型看一张位图更准，
且不花 LLM token、不依赖 vision_enabled。

两级取法（见 `lark-cli skills read lark-whiteboard references/lark-whiteboard-query.md`）：
1. `--output_as code` —— 画板里有且仅有一个 Mermaid/PlantUML 图时可导出源码，最理想；
2. `--output_as raw`  —— 飞书 OpenAPI 原生节点数组，本模块把它渲染成「节点 + 连线」大纲。

两步都拿不到就返回 None，由调用方（lark_fetcher）退回缩略图 + 视觉识别。
所有失败都吞掉转成 None —— 画板取不到不该拖垮整篇文档的导入。
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.config import get_settings

logger = logging.getLogger("caseweave.lark_whiteboard")

# raw 节点里连线（connector）自身也是一个 node，靠这些 key 关联首尾节点。
_CONNECTOR_ENDS = (("start_object", "start"), ("end_object", "end"))


async def fetch_whiteboard_text(
    token: str, *, timeout: float | None = None, identity: str | None = None,
) -> str | None:
    """把画板取成文字：优先 Mermaid/PlantUML 源码，其次「节点 + 连线」大纲。

    返回 None 表示两级都没拿到可用内容（无权限 / 空画板 / lark-cli 异常）。
    """
    if not token or not token.strip():
        return None

    settings = get_settings()
    timeout_sec = float(timeout if timeout is not None else settings.lark_cli_timeout_seconds)
    ident = identity or settings.lark_cli_identity

    payload = await _query(token, output_as="code", timeout_sec=timeout_sec, identity=ident)
    code = _extract_code(payload) if payload else None
    if code:
        logger.info("whiteboard %s: got %d chars of diagram code", token[:12], len(code))
        lang = "plantuml" if code.lstrip().lower().startswith("@start") else "mermaid"
        body = _clip(code, settings.lark_whiteboard_max_chars)
        return f"该画板的图表源码（{lang}）：\n```{lang}\n{body}\n```"

    payload = await _query(token, output_as="raw", timeout_sec=timeout_sec, identity=ident)
    nodes = _find_nodes(payload) if payload else None
    if nodes:
        outline = _outline_from_nodes(nodes)
        if outline:
            logger.info(
                "whiteboard %s: rendered outline from %d raw node(s)", token[:12], len(nodes),
            )
            return _clip(outline, settings.lark_whiteboard_max_chars)

    logger.info("whiteboard %s: no text extractable (will fall back to thumbnail)", token[:12])
    return None


# ── lark-cli 调用 ─────────────────────────────────────────────────────────────


async def _query(
    token: str, *, output_as: str, timeout_sec: float, identity: str,
) -> dict | str | None:
    """跑一次 `lark-cli whiteboard +query`，返回 stdout 解析结果（dict）或原始文本。

    不传 --output 时 code/raw 直接打到 stdout（image 才强制要求落盘）。
    """
    settings = get_settings()
    cli = settings.lark_cli_path or "lark-cli"
    cmd = [
        cli, "whiteboard", "+query",
        "--whiteboard-token", token,
        "--output_as", output_as,
        "--as", identity,
        "--format", "json",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning("whiteboard query: cannot spawn lark-cli (%s): %s", cli, exc)
        return None

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        logger.warning("whiteboard query timeout (%ss) token=%s as=%s", timeout_sec, token[:12], output_as)
        return None

    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")

    payload = _try_json(stdout)
    if isinstance(payload, dict) and payload.get("ok") is False:
        err = (payload.get("error") or {}).get("message") or "未知错误"
        logger.warning("whiteboard query failed token=%s as=%s: %s", token[:12], output_as, err)
        return None
    if payload is None:
        if proc.returncode != 0:
            logger.warning(
                "whiteboard query returncode=%d token=%s as=%s: %s",
                proc.returncode, token[:12], output_as, (stderr.strip() or stdout.strip())[:200],
            )
            return None
        # 极少数版本可能直接打裸文本（非 JSON）——code 模式下这本身就是我们要的东西
        return stdout.strip() or None
    return payload


# ── code 模式：抠 Mermaid / PlantUML 源码 ──────────────────────────────────────


_CODE_KEYS = ("code", "content", "mermaid", "plantuml", "text", "diagram", "source", "data")


def _extract_code(payload: dict | str) -> str | None:
    """从 code 模式返回里抠出图表源码。

    lark-cli 各版本的包裹层不完全一致（data.code / data.content / 裸串都见过），
    所以按候选 key 递归找第一个「像图表源码」的字符串。
    """
    if isinstance(payload, str):
        return payload if _looks_like_diagram(payload) else None

    found = _search_str(payload, _CODE_KEYS, depth=4)
    if found and _looks_like_diagram(found):
        return found.strip()
    return None


def _search_str(node, keys: tuple[str, ...], *, depth: int) -> str | None:
    """在嵌套 dict/list 里按 keys 优先级找第一个非空字符串（广度有限的保守搜索）。"""
    if depth < 0 or not isinstance(node, dict):
        return None
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v
    for v in node.values():
        if isinstance(v, dict):
            got = _search_str(v, keys, depth=depth - 1)
            if got:
                return got
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    got = _search_str(item, keys, depth=depth - 1)
                    if got:
                        return got
    return None


_DIAGRAM_MARKERS = (
    "@startuml", "graph ", "graph\n", "flowchart", "sequencediagram", "classdiagram",
    "statediagram", "mindmap", "erdiagram", "gantt", "journey", "pie ", "-->", "->",
)


def _looks_like_diagram(text: str) -> bool:
    """粗判是不是 Mermaid/PlantUML 源码 —— 防止把「画板不含代码」之类的提示语当成结果。"""
    s = (text or "").strip()
    if len(s) < 8:
        return False
    low = s.lower()
    if "不存在" in s or "no code" in low or "multiple" in low:
        return False
    return any(m in low for m in _DIAGRAM_MARKERS)


# ── raw 模式：节点数组 → 「节点 + 连线」大纲 ─────────────────────────────────────


def _find_nodes(payload: dict | str) -> list[dict] | None:
    """在 raw 返回里找出节点数组（形如 [{"id": ..., "type": ...}, ...]）。"""
    if isinstance(payload, str):
        payload = _try_json(payload) or {}
    if not isinstance(payload, dict):
        return None

    best: list[dict] | None = None

    def walk(node, depth: int) -> None:
        nonlocal best
        if depth < 0:
            return
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if dicts and sum(1 for d in dicts if "id" in d) >= max(1, len(dicts) // 2):
                if best is None or len(dicts) > len(best):
                    best = dicts
            for x in node:
                walk(x, depth - 1)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v, depth - 1)

    walk(payload, 6)
    return best


def _node_text(node: dict) -> str:
    """取节点文字：text 可能是 {"text": "..."} 也可能直接是字符串。"""
    for key in ("text", "label", "title"):
        v = node.get(key)
        if isinstance(v, str) and v.strip():
            return _clean(v)
        if isinstance(v, dict):
            for sub in ("text", "content", "plain_text"):
                sv = v.get(sub)
                if isinstance(sv, str) and sv.strip():
                    return _clean(sv)
    return ""


def _edges(nodes: list[dict]) -> list[tuple[str, str, str]]:
    """抽出连线：(起点 id, 终点 id, 连线自身文字/条件)。

    只管显式连线节点（connector）；思维导图那种靠 parent_id 串起来的父子关系交给 _tree_lines
    按缩进层级渲染 —— 拍平成 "A → B" 会因为同名节点（真实画板里「不做任何处理」能出现 5 次）
    彻底失去指向性。
    """
    out: list[tuple[str, str, str]] = []
    for n in nodes:
        if _parent_id(n):
            continue  # 思维导图父子关系由 _tree_lines 按层级渲染，不再重复成扁平连线
        conn = n.get("connector")
        src = conn if isinstance(conn, dict) else n
        ends: list[str] = []
        for key, _short in _CONNECTOR_ENDS:
            v = src.get(key)
            if isinstance(v, dict):
                # 连线可能吸附在节点上（attached_object_id），也可能只是坐标点（无 id）
                ident = v.get("id") or v.get("attached_object_id") or v.get("object_id")
                ends.append(str(ident) if ident else "")
            elif isinstance(v, str):
                ends.append(v)
            else:
                ends.append("")
        if len(ends) == 2 and (ends[0] or ends[1]):
            out.append((ends[0], ends[1], _node_text(n)))
    return out


def _parent_id(node: dict) -> str:
    """思维导图节点的父节点 id（实测在 mind_map.parent_id / mind_map_node.parent_id）。"""
    for key in ("mind_map", "mind_map_node"):
        v = node.get(key)
        if isinstance(v, dict):
            pid = v.get("parent_id")
            if pid:
                return str(pid)
    return ""


def _child_ids(node: dict) -> list[str]:
    """子节点 id 列表 —— mind_map_node.children 自带画板上的真实顺序，优先用它。"""
    v = node.get("mind_map_node")
    if isinstance(v, dict):
        children = v.get("children")
        if isinstance(children, list):
            return [str(c) for c in children if c]
    return []


def _tree_lines(nodes: list[dict], labels: dict[str, str]) -> tuple[list[str], set[str]]:
    """把有父子关系的节点渲染成缩进树，返回 (文本行, 已覆盖的节点 id)。

    思维导图靠层级表达分支，缩进树既保留了「谁属于谁」，又天然规避了同名节点在扁平
    "A → B" 里指代不清的问题。子节点顺序优先取 mind_map_node.children（画板真实顺序），
    缺失时回退按 (y, x) 排。
    """
    by_id = {str(n.get("id") or ""): n for n in nodes if n.get("id")}
    kids: dict[str, list[str]] = {}
    has_parent: set[str] = set()
    for nid, n in by_id.items():
        pid = _parent_id(n)
        if pid and pid in by_id:
            kids.setdefault(pid, []).append(nid)
            has_parent.add(nid)

    if not kids:
        return [], set()

    # children 数组给的顺序更可信；没有就按画板坐标从上到下
    for pid, children in kids.items():
        declared = [c for c in _child_ids(by_id[pid]) if c in by_id]
        rest = [c for c in children if c not in declared]
        rest.sort(key=lambda c: _xy(by_id[c])[::-1])
        kids[pid] = declared + rest

    roots = [nid for nid in by_id if nid not in has_parent and (nid in kids or labels.get(nid))]
    roots = [r for r in roots if r in kids]  # 孤立无子节点的不算树根，留给「其他节点」
    roots.sort(key=lambda r: _xy(by_id[r])[::-1])

    lines: list[str] = []
    covered: set[str] = set()

    def walk(nid: str, depth: int) -> None:
        if nid in covered or depth > 20:  # depth 上限兼防环
            return
        covered.add(nid)
        lines.append(f"{'  ' * depth}- {labels.get(nid) or '（无文字节点）'}")
        for child in kids.get(nid, []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    return lines, covered


def _outline_from_nodes(nodes: list[dict]) -> str:
    """把节点数组渲染成人类/LLM 都好读的文本：层级树 +（流程图的）连线走向 + 其余节点。

    没有父子关系的节点按 (y, x) 排 —— 画板上从上到下、从左到右，通常即阅读顺序。
    """
    labels: dict[str, str] = {}
    plain: list[tuple[float, float, str, str]] = []  # (y, x, id, text)
    for n in nodes:
        nid = str(n.get("id") or "")
        txt = _node_text(n)
        if nid and txt:
            labels[nid] = txt
        # 连线本身不进节点清单（它的文字是条件标签，随连线一起输出）
        if txt and not _is_connector(n):
            x, y = _xy(n)
            plain.append((y, x, nid, txt))

    tree, covered = _tree_lines(nodes, labels)
    edges = _edges(nodes)
    plain = [t for t in plain if t[2] not in covered]
    if not tree and not plain and not edges:
        return ""

    lines: list[str] = []
    if tree:
        lines.append(f"层级结构（共 {len(covered)} 个节点，缩进表示从属关系）：")
        lines.extend(tree)

    # 顺序：层级树 → 节点清单 → 连线走向。流程图（无树）时即「先有哪些节点、再看怎么连」。
    if plain:
        plain.sort(key=lambda t: (t[0], t[1]))
        if lines:
            lines.append("")
        head = "其他节点" if tree else "节点"
        lines.append(f"{head}（共 {len(plain)} 个，按画板从上到下、从左到右排列）：")
        lines.extend(f"- {txt}" for _y, _x, _nid, txt in plain)

    rendered = [
        f"- {labels.get(a) or '（无名节点）'} → {labels.get(b) or '（无名节点）'}"
        + (f"（{cond}）" if cond else "")
        for a, b, cond in edges
        if labels.get(a) or labels.get(b)
    ]
    if rendered:
        if lines:
            lines.append("")
        lines.append(f"连线走向（共 {len(rendered)} 条，箭头即流程方向）：")
        lines.extend(rendered)

    return "\n".join(lines).strip()


def _xy(node: dict) -> tuple[float, float]:
    """节点坐标 (x, y)；缺失或非法一律当 0。"""
    try:
        return float(node.get("x") or 0), float(node.get("y") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def _is_connector(node: dict) -> bool:
    if isinstance(node.get("connector"), dict):
        return True
    t = node.get("type")
    return isinstance(t, str) and "connector" in t.lower()


# ── helpers ──────────────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    return " ".join(str(text).split()).strip()


def _clip(text: str, limit: int) -> str:
    s = (text or "").strip()
    if limit > 0 and len(s) > limit:
        return s[:limit].rstrip() + f"\n…（内容过长已截断，原文 {len(s)} 字）"
    return s


def _try_json(stdout: str) -> dict | None:
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
