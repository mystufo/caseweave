"""新增 system_settings 表：管理员在网页上覆盖 .env 的运行时配置（LLM / 视觉两组）。

Revision ID: 0009_system_settings
Revises: 0008_daily_usage
Create Date: 2026-09-06

见 app/settings_store.py。fresh DB 经 init_db + create_all 已建好，IF NOT EXISTS 保证这里 no-op；
只有 stamp 过 baseline 的老库才真正建表。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0009_system_settings"
down_revision: Union[str, None] = "0008_daily_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key         VARCHAR(64) PRIMARY KEY,
            value       TEXT NOT NULL,
            is_secret   BOOLEAN NOT NULL DEFAULT false,
            updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_at  TIMESTAMPTZ DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS system_settings")
