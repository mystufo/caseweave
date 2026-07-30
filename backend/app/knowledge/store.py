"""Knowledge store: 写入 / 语义检索 KnowledgeEntry。

Phase 3 的核心：把"用户上传文档"积累成可被未来检索的产品上下文。
- store_entries：批量写入，可选 embedding（无 embedding 时仍然落库，仅跳过向量字段）
- search_relevant：项目内按 module_id 可选过滤，向量距离近邻 + 按 confidence 加权
- summarize_for_prompt：把命中条目拼成给 Clarifier/Generator 的 prompt 片段

为什么不在调用方各自处理：embedding 失败、维度对不上、pgvector 缺失都是"应当容忍"的故障，
集中在 store 里降级，调用方只关心拿到/拿不到知识。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeEntry
from app.tools.embeddings import embed_texts, embed_one
from app.tools.reranker import rerank
from app.tools.text_chunking import split_for_embedding
from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeDraft:
    """A knowledge entry waiting to be embedded + persisted."""
    knowledge_type: str   # product_rule / module_relation / defect_pattern / term / constraint
    content: str          # 自然语言或结构化 JSON-as-text
    source: str = "document"
    confidence: float = 0.6


async def store_entries(
    db: AsyncSession,
    *,
    project_id: int,
    module_id: Optional[int],
    document_id: Optional[int],
    drafts: list[KnowledgeDraft],
) -> int:
    """Embed (best-effort) + persist drafts. Returns number of rows inserted.

    embedding 不可用时仍会写入（向量字段留 NULL），保证文档积累能继续。

    去重策略（避免同一份知识被反复抽取后多次入库，污染检索召回）：
    1. 精确文本去重：本项目下（同 module_id 域；project-level NULL 也参与对比）已存在 content 完全相同的条目 → 跳过
    2. 语义去重：embedding 可用时，与已有向量的余弦距离 < 0.05 → 视为同义重复，跳过
    跳过的条目记一行 info 日志便于排查。
    """
    drafts = [d for d in drafts if (d.content or "").strip()]
    if not drafts:
        return 0

    # ── Step 1：精确文本去重 ───────────────────────────────────────────────
    # 取出本项目下、与本次入库 module 相同（或 NULL/相同 module 都算）的现有 content 集合
    exist_stmt = select(KnowledgeEntry.content).where(KnowledgeEntry.project_id == project_id)
    if module_id is None:
        exist_stmt = exist_stmt.where(KnowledgeEntry.module_id.is_(None))
    else:
        exist_stmt = exist_stmt.where(
            (KnowledgeEntry.module_id == module_id) | (KnowledgeEntry.module_id.is_(None))
        )
    existing_contents = {
        (c or "").strip() for c in (await db.execute(exist_stmt)).scalars().all()
    }
    seen_in_batch: set[str] = set()
    deduped: list[KnowledgeDraft] = []
    for d in drafts:
        key = d.content.strip()
        if key in existing_contents or key in seen_in_batch:
            logger.info("store_entries: skip exact duplicate (%s) %s", d.knowledge_type, key[:60])
            continue
        seen_in_batch.add(key)
        deduped.append(d)
    if not deduped:
        return 0

    # ── Step 2：embedding + 语义去重 ──────────────────────────────────────
    vectors = await embed_texts([d.content for d in deduped])
    if vectors is None:
        logger.info(
            "store_entries: embedding unavailable, persisting %d entries without vectors",
            len(deduped),
        )

    inserted = 0
    for i, d in enumerate(deduped):
        vec = vectors[i] if vectors is not None else None
        if vec is not None:
            try:
                # 找最近的同项目（本 module 或 NULL）已有条目
                near_sql = text(
                    """
                    SELECT id, embedding <=> CAST(:qvec AS vector) AS distance
                    FROM knowledge_entries
                    WHERE project_id = :project_id
                      AND embedding IS NOT NULL
                      AND (CAST(:module_id AS INTEGER) IS NULL
                           OR module_id = CAST(:module_id AS INTEGER)
                           OR module_id IS NULL)
                    ORDER BY distance ASC
                    LIMIT 1
                    """
                )
                row = (await db.execute(
                    near_sql,
                    {"qvec": str(vec), "project_id": project_id, "module_id": module_id},
                )).first()
                if row is not None and row[1] is not None and float(row[1]) < 0.05:
                    logger.info(
                        "store_entries: skip near-duplicate (dist=%.3f) %s",
                        float(row[1]), d.content[:60],
                    )
                    continue
            except Exception as e:
                # pgvector 缺失或临时故障——退回到只做文本去重，继续插入
                logger.warning("near-duplicate check failed, inserting anyway: %s", e)
                try:
                    await db.rollback()
                except Exception:
                    pass

        entry = KnowledgeEntry(
            project_id=project_id,
            module_id=module_id,
            document_id=document_id,
            knowledge_type=d.knowledge_type,
            content=d.content,
            source=d.source,
            confidence=d.confidence,
            embedding=vec,
        )
        db.add(entry)
        inserted += 1
    if inserted > 0:
        await db.commit()
    return inserted


@dataclass
class HitEntry:
    id: int
    knowledge_type: str
    content: str
    confidence: float
    distance: Optional[float]   # cosine distance, 越小越相关；fallback 路径为 None


@dataclass
class NearMiss:
    """未命中时"差一点入选"的候选：命中数据 + 落选原因（供前端只读展示）。"""
    id: int
    knowledge_type: str
    content: str
    confidence: float
    distance: Optional[float]
    reason: str   # "超阈值>0.45" / "同文档排除" / "本应入选" 等，可用｜分隔多个


async def search_relevant(
    db: AsyncSession,
    *,
    project_id: int,
    module_id: Optional[int],
    query: str,
    top_k: int = 6,
    distance_threshold: Optional[float] = None,
) -> list[HitEntry]:
    """Find top-K most relevant knowledge entries for a query.

    两阶段检索（都在本函数内部，签名保持稳定）：
    1. 召回（分块 max-sim）：把 query 切成窗口分别 embed，对每个窗口各跑一次 pgvector 近邻，
       按条目取「跨所有窗口的最小余弦距离」，得到 rerank_candidate_k 大小的候选池。
       解决「整篇文档平均向量」把单句知识相关度稀释的问题——一条知识只要和文档某一段高度
       相似即可进池。
    2. 精排（cross-encoder rerank，可选）：把 query 与候选逐条拼一起打分，按分数排序取 top_k，
       并把 distance 记为 1-score（与前端「相关度%=1-distance」口径一致）。rerank 不可用/关闭
       时退回按余弦距离排序过滤（召回改进仍生效）。

    逐级降级（fail-open，绝不阻塞主流程）：
      embedding 可用 + rerank 可用 → 分块召回 + 精排
      embedding 可用 + rerank 不可用/关闭 → 分块召回 + 距离阈值
      embedding 不可用 → 按 confidence DESC, created_at DESC 取最新若干条

    distance_threshold:
      None → 用生效的自然阈值（精排时 1-rerank_score_threshold；否则 knowledge_distance_threshold）
      <= 0 → 不过滤（log_miss_diagnostics 借此拿完整候选池）
      > 0  → distance > 该值的命中丢弃（仅向量路径有效；fallback 路径 distance=None 不过滤）
    """
    query = (query or "").strip()
    if not query:
        logger.info(
            "knowledge search project=%s module=%s | query 为空，直接返回 0 条",
            project_id, module_id,
        )
        return []

    s = get_settings()
    candidate_k = max(top_k, s.rerank_candidate_k)

    # ── 阶段 1：分块 max-sim 召回 ─────────────────────────────────────────────
    chunks = split_for_embedding(
        query,
        size=s.knowledge_query_chunk_size,
        overlap=s.knowledge_query_chunk_overlap,
    )
    vectors = await embed_texts(chunks) if chunks else None

    if vectors:
        # 对每个 chunk 向量各跑一次近邻，Python 端按 id 取最小距离合并成候选池。
        # 模块过滤用 OR 让 module_id IS NULL 的项目级规则也参与。
        # NOTE: asyncpg prepared statement 无法从 `:module_id IS NULL` 推断 NULL 参数类型
        # （AmbiguousParameterError），所有可能为 NULL 的参数都显式 CAST。
        sql = text(
            """
            SELECT id, knowledge_type, content, confidence,
                   embedding <=> CAST(:qvec AS vector) AS distance
            FROM knowledge_entries
            WHERE project_id = :project_id
              AND embedding IS NOT NULL
              AND (CAST(:module_id AS INTEGER) IS NULL
                   OR module_id = CAST(:module_id AS INTEGER)
                   OR module_id IS NULL)
            ORDER BY distance ASC
            LIMIT :cand_k
            """
        )
        try:
            best: dict[int, HitEntry] = {}
            for vec in vectors:
                rows = (await db.execute(
                    sql,
                    {"qvec": str(vec), "project_id": project_id,
                     "module_id": module_id, "cand_k": candidate_k},
                )).all()
                for r in rows:
                    dist = float(r[4])
                    prev = best.get(r[0])
                    if prev is None or dist < (prev.distance if prev.distance is not None else 1e9):
                        best[r[0]] = HitEntry(
                            id=r[0], knowledge_type=r[1], content=r[2],
                            confidence=float(r[3] or 0.0), distance=dist,
                        )
            candidates = sorted(best.values(), key=lambda h: h.distance if h.distance is not None else 1e9)
            candidates = candidates[:candidate_k]

            if candidates:
                # ── 阶段 2：cross-encoder 精排（可选，fail-open）──────────────
                scores = await rerank(query, [h.content for h in candidates])
                reranked = scores is not None
                if reranked:
                    for h, sc in zip(candidates, scores):
                        h.distance = 1.0 - float(sc)   # 归一到与余弦同向的「越小越相关」口径
                    candidates.sort(key=lambda h: h.distance if h.distance is not None else 1e9)

                # 生效阈值：显式传入优先；否则精排用 1-score_threshold，纯向量用距离阈值
                if distance_threshold is None:
                    gate = (1.0 - s.rerank_score_threshold) if reranked else s.knowledge_distance_threshold
                else:
                    gate = distance_threshold

                before = len(candidates)
                hits = candidates
                if gate and gate > 0:
                    hits = [h for h in hits if h.distance is not None and h.distance <= gate]
                hits = hits[:top_k]
                logger.info(
                    "knowledge search[vector] project=%s module=%s chunks=%d rerank=%s "
                    "top_k=%s gate=%.3f | 候选=%d 阈值后=%d | 距离(id:dist)=%s",
                    project_id, module_id, len(chunks), reranked,
                    top_k, gate if gate else 0.0,
                    before, len(hits),
                    ", ".join(f"{h.id}:{h.distance:.3f}" for h in candidates[:top_k]),
                )
                return hits
            # 落空就走 fallback —— 全新项目第一篇文档时尤其常见
            logger.info(
                "knowledge search[vector] project=%s module=%s chunks=%d top_k=%s | 向量近邻 0 条命中，转 fallback",
                project_id, module_id, len(chunks), top_k,
            )
        except Exception as e:
            logger.warning("vector search failed, falling back to recency: %s", e)
            # raw SQL 报错时 asyncpg 会把 session 标成 aborted，后续 ORM 查询都会报
            # "current transaction is aborted"。必须 rollback 把脏事务清掉。
            try:
                await db.rollback()
            except Exception:
                pass

    # Fallback: 按 confidence 倒序拿最近的若干条
    stmt = (
        select(KnowledgeEntry)
        .where(KnowledgeEntry.project_id == project_id)
        .order_by(KnowledgeEntry.confidence.desc(), KnowledgeEntry.created_at.desc())
        .limit(top_k)
    )
    if module_id is not None:
        stmt = stmt.where(
            (KnowledgeEntry.module_id == module_id) | (KnowledgeEntry.module_id.is_(None))
        )
    rows = (await db.execute(stmt)).scalars().all()
    logger.info(
        "knowledge search[fallback] project=%s module=%s top_k=%s embedding=%s | 命中=%d 条(按 confidence/时间倒序)",
        project_id, module_id, top_k, bool(vectors), len(rows),
    )
    return [HitEntry(
        id=e.id, knowledge_type=e.knowledge_type, content=e.content,
        confidence=float(e.confidence or 0.0), distance=None,
    ) for e in rows]


@dataclass
class SimilarEntry:
    """与某条候选 draft 高度相关的已有 KnowledgeEntry —— 用于"潜在冲突"提示。"""
    entry_id: int
    knowledge_type: str
    content: str
    confidence: float
    distance: float   # 余弦距离


async def find_similar_entries(
    db: AsyncSession,
    *,
    project_id: int,
    module_id: Optional[int],
    content: str,
    top_k: int = 5,
    min_distance: float = 0.0,
    max_distance: float = 0.30,
) -> list[SimilarEntry]:
    """找出和 content 语义近似的已有条目（含完全重复项）。

    这里返回 [min_distance, max_distance] 区间内的"邻居"，交给上层 LLM 判定
    与草稿的关系（duplicate / similar / conflict / unrelated）。

    注意：min_distance 默认 0.0，让**完全重复**（距离极小）的条目也能进候选——
    去重不再在检索层用阈值一刀切，而是交给 LLM 语义判定（区分"重复"与"相似"）。
    top_k 略放宽到 5，覆盖多条相关旧条目。

    无 embedding 服务 / 没有命中 / pgvector 异常 → 返回空列表（不抛）。
    """
    text_q = (content or "").strip()
    if not text_q:
        return []
    qvec = await embed_one(text_q)
    if qvec is None:
        return []

    sql = text(
        """
        SELECT id, knowledge_type, content, confidence,
               embedding <=> CAST(:qvec AS vector) AS distance
        FROM knowledge_entries
        WHERE project_id = :project_id
          AND embedding IS NOT NULL
          AND (CAST(:module_id AS INTEGER) IS NULL
               OR module_id = CAST(:module_id AS INTEGER)
               OR module_id IS NULL)
        ORDER BY distance ASC
        LIMIT :top_k
        """
    )
    try:
        rows = (await db.execute(
            sql,
            {"qvec": str(qvec), "project_id": project_id,
             "module_id": module_id, "top_k": top_k},
        )).all()
    except Exception as e:
        logger.warning("find_similar_entries failed: %s", e)
        try:
            await db.rollback()
        except Exception:
            pass
        return []

    out: list[SimilarEntry] = []
    for r in rows:
        d = float(r[4]) if r[4] is not None else 1.0
        if d < min_distance or d > max_distance:
            continue
        out.append(SimilarEntry(
            entry_id=r[0], knowledge_type=r[1], content=r[2],
            confidence=float(r[3] or 0.0), distance=d,
        ))
    return out


async def log_miss_diagnostics(
    db: AsyncSession,
    *,
    project_id: int,
    module_id: Optional[int],
    document_id: Optional[int],
    query: str,
    top_k: int,
    context: str = "",
) -> list[NearMiss]:
    """前端面板判定"知识库未命中"时，把被丢弃的前 top_k 相关条目打进日志并返回，方便排查/展示。

    重新以 distance_threshold=0（不过滤）跑一次近邻，专供排查：看清楚"差一点命中"
    的是哪几条、余弦距离多少、为何没进面板（超距离阈值 / 与本文档同源被排除）。
    仅在未命中时触发，失败绝不影响主流程（只 warning，返回 []）。

    返回值供前端只读展示（携带 reason 落选原因），同时照常写一行诊断日志。

    context: 调用点标识（如 "upload" / "rest"），拼进日志便于区分来源。
    document_id: 给出时，把"与本文档同源"的候选标注为"同文档排除"。
    """
    try:
        cand = await search_relevant(
            db, project_id=project_id, module_id=module_id,
            query=query, top_k=top_k, distance_threshold=0,
        )
    except Exception as exc:
        logger.warning("knowledge miss diagnostics failed: %s", exc)
        return []
    tag = f"[{context}]" if context else ""
    if not cand:
        logger.info(
            "knowledge miss%s project=%s module=%s doc=%s | 无任何候选（向量库为空或 embedding 不可用）",
            tag, project_id, module_id, document_id,
        )
        return []
    # 找出其中"与本文档同源"的条目 id —— 这类会被同文档排除逻辑剔掉
    same_doc: set[int] = set()
    if document_id is not None:
        try:
            rows = (await db.execute(
                select(KnowledgeEntry.id).where(
                    KnowledgeEntry.id.in_([h.id for h in cand]),
                    KnowledgeEntry.document_id == document_id,
                )
            )).scalars().all()
            same_doc = set(rows)
        except Exception:
            pass
    s = get_settings()
    # near-miss 里的 distance 与 search_relevant 命中同口径：精排开启时是 1-score，
    # 落选门相应为 1-rerank_score_threshold；否则用余弦距离阈值。文案随之自适应。
    if s.rerank_enabled:
        gate = 1.0 - s.rerank_score_threshold
        gate_label = f"低于精排阈值<{s.rerank_score_threshold}"
    else:
        gate = s.knowledge_distance_threshold
        gate_label = f"超阈值>{gate}"
    lines: list[str] = []
    misses: list[NearMiss] = []
    for i, h in enumerate(cand, 1):
        dist = f"{h.distance:.3f}" if h.distance is not None else "NA"
        reasons: list[str] = []
        if h.distance is not None and gate and gate > 0 and h.distance > gate:
            reasons.append(gate_label)
        if h.id in same_doc:
            reasons.append("同文档排除")
        why = "｜".join(reasons) if reasons else "本应入选"
        snippet = (h.content or "").replace("\n", " ")[:80]
        lines.append(f"    #{i} id={h.id} dist={dist} [{why}] [{h.knowledge_type}] {snippet}")
        misses.append(NearMiss(
            id=h.id, knowledge_type=h.knowledge_type, content=h.content,
            confidence=h.confidence, distance=h.distance, reason=why,
        ))
    logger.info(
        "knowledge miss%s project=%s module=%s doc=%s gate=%s | 被丢弃的前%d相关条目:\n%s",
        tag, project_id, module_id, document_id, gate, len(cand), "\n".join(lines),
    )
    return misses


def summarize_for_prompt(hits: list[HitEntry], max_chars: Optional[int] = None) -> str:
    """把命中条目拼成 Clarifier/Generator prompt 用的 plain-text 片段。

    格式有意保持简短：每条一行，类型+内容，避免把原始 JSON 直接灌给 LLM。
    超长截断（按字符），保证不撑破上下文窗口。

    max_chars: None → 用 settings.knowledge_prompt_max_chars（默认 1800）
    """
    if not hits:
        return ""
    if max_chars is None:
        max_chars = get_settings().knowledge_prompt_max_chars
    lines: list[str] = []
    used = 0
    for h in hits:
        line = f"- [{h.knowledge_type}] {h.content}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
