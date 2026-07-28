"""Alembic env using the project's async engine + Settings.

设计要点：
- DB URL 从 app.config.get_settings 读，避免在 alembic.ini 重复维护
- 用我们运行时的 async engine（asyncpg）跑 migration，不另开 sync engine —— 一份连接配置
- target_metadata 指 Base.metadata，所有模型在 init_db / 这里都通过同一个 import 列表注册
"""
from __future__ import annotations

import asyncio
import logging.config
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.database import Base
# 强制 import 所有模型，让 Base.metadata 收齐表
from app.models import session, knowledge, feedback, user, clarification  # noqa: F401

config = context.config
# 仅当独立跑 alembic CLI（root logger 还没配置 handler）时才让 alembic 接管日志。
# 在应用进程内启动迁移时，root 已经被 setup_logging() 挂好了 stderr + 文件 handler；
# fileConfig() 默认会清空 root 的 handlers（摘掉我们的 RotatingFileHandler）并
# disable_existing_loggers=True 禁用所有 caseweave.* / uvicorn logger —— 那样迁移之后
# 全站日志（含请求日志、知识检索诊断）都不再落盘。此处跳过即可保住应用自己的日志配置。
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

# Wire DB URL from project settings
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without DB connection. autogenerate 这种用法不支持，但 -> SQL 可。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # pgvector 列类型在 autogenerate 时容易触发"列类型变更"假阳性，关掉服务端默认值对比
        compare_server_default=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online_async() -> None:
    """Run migrations using async engine (asyncpg)."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_migrations_online_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
