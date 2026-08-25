from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def _run_alembic_upgrade_head() -> None:
    """启动时把 alembic 推到 head：

    - 现有 schema 已与 init_db 对齐时，第一次会"自动 stamp"到 baseline（见下面的 ensure_baseline_stamp）
    - 之后增量 migration 都通过这里跑（init_db 不再加新 ALTER）
    - 任何异常都吞掉但写 warning 日志——alembic 失败不该阻塞服务启动，但需要被监控发现
    """
    import logging as _lg
    log = _lg.getLogger("caseweave.database")
    import os as _os
    if _os.environ.get("ALEMBIC_SKIP") == "1":
        log.info("alembic upgrade skipped via ALEMBIC_SKIP=1")
        return
    try:
        # alembic Config 在 sync 上下文里跑；用线程池避免阻塞 event loop
        import asyncio as _asyncio
        from pathlib import Path

        from alembic import command
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
        if not ini_path.exists():
            log.warning("alembic.ini not found at %s, skipping migrations", ini_path)
            return

        def _run() -> None:
            cfg = Config(str(ini_path))
            # alembic 是同步的，不支持 asyncpg —— 必须把 URL 改回 psycopg2 driver。
            # 否则 command.stamp/upgrade 内部 import asyncpg 会卡死或行为异常。
            sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
            cfg.set_main_option("sqlalchemy.url", sync_url)
            # ensure_baseline_stamp：表已存在但 alembic_version 表为空 → 视为 baseline schema
            # 已就绪，stamp 到 baseline 让 upgrade 成为幂等 no-op；之后再追新 migration 才真跑
            from sqlalchemy import create_engine
            sync_engine = create_engine(sync_url, pool_pre_ping=True)
            with sync_engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                current = ctx.get_current_revision()
                script = ScriptDirectory.from_config(cfg)
                head = script.get_current_head()
                # 表已存在（init_db 跑过）+ alembic 没记录过 → stamp baseline
                if current is None:
                    has_sessions = conn.exec_driver_sql(
                        "SELECT 1 FROM information_schema.tables WHERE table_name='sessions'"
                    ).first()
                    if has_sessions and head:
                        log.info(
                            "alembic: schema present but unstamped → stamping baseline=%s",
                            head,
                        )
                        command.stamp(cfg, head)
                        return
            sync_engine.dispose()
            command.upgrade(cfg, "head")

        await _asyncio.to_thread(_run)
        log.info("alembic upgrade head: ok")
    except Exception as exc:
        log.warning("alembic upgrade failed (non-fatal): %s", exc)


async def init_db():
    """Create all tables on startup; apply ad-hoc migrations for multi-tenancy rollout.

    长期目标：所有 schema 变更走 alembic（见 alembic/versions/）。当前 init_db 仍然跑，
    用于兜底"全新空库"和"已有部署 stamp 到 baseline"两种场景。新加列请只放在新的
    migration 文件，不要继续往这里加 ALTER TABLE。
    """
    # Import all models so Base knows about them
    from app.models import session, knowledge, feedback, user, clarification, usage  # noqa: F401
    from sqlalchemy import text

    async with engine.begin() as conn:
        # Phase 3: 启用 pgvector 扩展（必须先于 create_all，因为 KnowledgeEntry.embedding 列类型依赖它）
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        # Multi-tenancy migration: legacy rows have no project_id, drop the affected
        # tables so create_all rebuilds them with the new schema. User opted for "清空重来".
        await conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='sessions')
                   AND NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='sessions' AND column_name='project_id'
                   )
                THEN
                    DROP TABLE IF EXISTS
                        feedbacks, test_cases, messages, sessions,
                        knowledge_entries, module_relations, documents, skills, modules
                    CASCADE;
                END IF;
            END $$;
        """))

        # Phase 3 schema 变更：knowledge_entries 加 project_id / document_id；module_id 改可空 + 列类型从 JSON 切到 vector(N)。
        # 历史 Phase 1/2 部署的此表是空的（Phase 3 之前没人写知识条目），直接 DROP 重建省事。
        # 另：当 settings.embedding_dim 与现有 embedding 列维度不一致时也整表 DROP（pgvector 的 vector(N) 维度
        # 是写死的，改维度等于换列类型，最干净的做法就是重建——反正知识条目可由 documents.raw_text 重新抽取。
        from app.config import get_settings as _gs
        _dim = _gs().embedding_dim
        await conn.execute(text(f"""
            DO $$
            DECLARE
                cur_atttypmod int;
                cur_dim int;
            BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='knowledge_entries') THEN
                    -- 1) Phase 1/2 历史空表：缺 project_id 列
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='knowledge_entries' AND column_name='project_id'
                    ) THEN
                        DROP TABLE IF EXISTS knowledge_entries CASCADE;
                    ELSE
                        -- 2) 维度变化：从 pg_attribute.atttypmod 反推 vector 列维度，对比 settings
                        SELECT a.atttypmod INTO cur_atttypmod
                          FROM pg_attribute a
                          JOIN pg_class c ON a.attrelid = c.oid
                         WHERE c.relname = 'knowledge_entries' AND a.attname = 'embedding';
                        -- pgvector 的 atttypmod 就是维度本身（不像普通类型那样要减 4）
                        cur_dim := cur_atttypmod;
                        IF cur_dim IS NOT NULL AND cur_dim > 0 AND cur_dim <> {_dim} THEN
                            RAISE NOTICE 'knowledge_entries embedding dim mismatch: existing=%, new={_dim}; dropping table for rebuild', cur_dim;
                            DROP TABLE IF EXISTS knowledge_entries CASCADE;
                        END IF;
                    END IF;
                END IF;
            END $$;
        """))

        await conn.run_sync(Base.metadata.create_all)

        # Phase 3 索引：按 project + module 过滤后做余弦近邻。ivfflat 需要数据先存在再 build，
        # lists 取小一点让冷启动也能用；后续数据量上来可以 REINDEX 提高 lists。
        # pgvector 的 ivfflat / hnsw 索引硬上限是 2000 维 —— 超过就只能顺序扫描（对几十~几百条
        # 规模的知识库完全够用，毫秒级返回；当数据量到十万级时再考虑切 halfvec(hnsw) 或降维）。
        if _dim <= 2000:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_entries_embedding "
                "ON knowledge_entries USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50)"
            ))
        else:
            # 主动 drop 老索引（如果维度从 ≤2000 升到 >2000，索引可能还在）
            await conn.execute(text("DROP INDEX IF EXISTS ix_knowledge_entries_embedding"))
            import logging as _lg
            _lg.getLogger("caseweave.database").info(
                "knowledge_entries embedding dim=%d > 2000, skipping ivfflat index (sequential scan)", _dim,
            )

        # Lightweight ad-hoc migrations (no Alembic yet)
        await conn.execute(text(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS sha256 VARCHAR(64)"
        ))
        await conn.execute(text(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS clarification JSONB"
        ))
        # Drop the global-unique sha256 index if it survives from before multi-tenancy.
        await conn.execute(text(
            "DROP INDEX IF EXISTS ix_documents_sha256"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_documents_sha256 ON documents(sha256)"
        ))

        # Priority column rolled out after the multi-tenancy migration; backfill existing rows to P2.
        await conn.execute(text(
            "ALTER TABLE test_cases ADD COLUMN IF NOT EXISTS priority VARCHAR(2)"
        ))
        await conn.execute(text(
            "UPDATE test_cases SET priority='P2' WHERE priority IS NULL"
        ))

        # Lark doc URL import — source columns (rolled out after file-only era).
        # file_type widened from VARCHAR(10) to (20) to fit lark_wiki / lark_sheet.
        await conn.execute(text(
            "ALTER TABLE documents ALTER COLUMN file_type TYPE VARCHAR(20)"
        ))
        await conn.execute(text(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_type VARCHAR(10) NOT NULL DEFAULT 'file'"
        ))
        await conn.execute(text(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_url VARCHAR(500)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_documents_source_url ON documents(source_url)"
        ))

        # B 方案：澄清前先让用户预览/勾选知识库条目，需要新状态 "awaiting_clarification"（22 字符），
        # 老 status 列原宽 VARCHAR(20) 装不下。直接放宽到 40 给以后留余量。
        await conn.execute(text(
            "ALTER TABLE clarification_states ALTER COLUMN status TYPE VARCHAR(40)"
        ))

        # 测试脑图支持：documents 增加 role 列，clarification_states 增加 mindmap_document_id 列。
        # 历史行 role 默认 'prd'（向后兼容，现有 PRD 流程零改动）。
        await conn.execute(text(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'prd'"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_documents_role ON documents(role)"
        ))
        await conn.execute(text(
            "ALTER TABLE clarification_states ADD COLUMN IF NOT EXISTS mindmap_document_id INTEGER "
            "REFERENCES documents(id) ON DELETE SET NULL"
        ))

        # 知识抽取人工确认闸门：抽取出来的草稿先写到 documents.pending_knowledge JSONB，
        # 用户在前端审核勾选后才入 knowledge_entries。详见 routes_upload.py 抽取流程。
        await conn.execute(text(
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS pending_knowledge JSONB"
        ))

    # init_db 完成后，把 alembic 推到 head：
    # - 既有部署：第一次会 stamp 到 baseline（0001_phase3_baseline），之后增量 migration 才生效
    # - 新部署：init_db 上面已建好表，alembic 也只是 stamp baseline，不会重复建表
    # - 失败不阻塞启动（在 _run_alembic_upgrade_head 内部 try/except）
    await _run_alembic_upgrade_head()
