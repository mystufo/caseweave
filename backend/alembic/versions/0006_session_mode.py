"""sessions 加 mode 列（cases / mindmap）。

Revision ID: 0006_session_mode
Revises: 0005_feedback_triage
Create Date: 2026-07-15

对话页顶层区分「生成测试用例 / 生成测试脑图」两种会话用途。
fresh DB（init_db + create_all）已按新模型建表，IF NOT EXISTS 保证 no-op；
只有已 stamp baseline 的老库才真正 ADD。老会话默认 'cases'，行为不变。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0006_session_mode"
down_revision: Union[str, None] = "0005_feedback_triage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS mode VARCHAR(16) NOT NULL DEFAULT 'cases'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS mode")
