"""Phase 4.2 二阶段：新增 prompt_suggestions 表（系统给的 prompt 改进建议草稿）。

Revision ID: 0004_prompt_suggestions
Revises: 0003_module_code
Create Date: 2026-07-06

系统（手动按钮 / 定期后台）分析负反馈后，把「建议的 prompt 改动」以 pending 草稿形式
落到本表，等人工审核。本表只记建议与状态，绝不改任何生效版本——采用建议仍走一阶段
已有的 POST /prompts/{key}/versions。

fresh DB（init_db + create_all）已按新模型建表，CREATE TABLE IF NOT EXISTS 保证对其
no-op；只有已 stamp baseline 的老库才真正建表。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0004_prompt_suggestions"
down_revision: Union[str, None] = "0003_module_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_suggestions (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL,
            prompt_id VARCHAR(100) NOT NULL,
            base_version_id INTEGER,
            base_template TEXT NOT NULL,
            suggested_template TEXT NOT NULL,
            rationale TEXT,
            evidence JSON,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT now(),
            reviewed_by INTEGER,
            reviewed_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_prompt_suggestions_project_id "
        "ON prompt_suggestions (project_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_prompt_suggestions_prompt_id "
        "ON prompt_suggestions (prompt_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_prompt_suggestions_status "
        "ON prompt_suggestions (status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS prompt_suggestions")
