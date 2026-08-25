"""新增 daily_usage 表：按用户 × 自然日累计 LLM token 用量。

Revision ID: 0008_daily_usage
Revises: 0007_project_is_public
Create Date: 2026-08-24

支撑「每日 token 配额」这层成本封顶（见 app/limits.py / app/usage.py）。
fresh DB 经 init_db + create_all 已建好，IF NOT EXISTS 保证这里 no-op；
只有 stamp 过 baseline 的老库才真正建表。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0008_daily_usage"
down_revision: Union[str, None] = "0007_project_is_public"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            id             SERIAL PRIMARY KEY,
            user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            day            DATE NOT NULL,
            input_tokens   BIGINT NOT NULL DEFAULT 0,
            output_tokens  BIGINT NOT NULL DEFAULT 0,
            calls          INTEGER NOT NULL DEFAULT 0,
            created_at     TIMESTAMPTZ DEFAULT now(),
            updated_at     TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_daily_usage_user_day UNIQUE (user_id, day)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_daily_usage_user_id ON daily_usage(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_daily_usage_day ON daily_usage(day)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_usage")
