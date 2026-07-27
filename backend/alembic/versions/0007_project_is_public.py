"""projects 加 is_public 列（公开 / 私有可见性）。

Revision ID: 0007_project_is_public
Revises: 0006_session_mode
Create Date: 2026-07-27

公开项目对所有用户可见；私有项目仅创建者本人和管理员可见。
fresh DB（init_db + create_all）已按新模型建表，IF NOT EXISTS 保证 no-op；
只有已 stamp baseline 的老库才真正 ADD。存量项目默认私有（FALSE），
需要公开的由创建者或管理员在界面上手动切换。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0007_project_is_public"
down_revision: Union[str, None] = "0006_session_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS is_public")
