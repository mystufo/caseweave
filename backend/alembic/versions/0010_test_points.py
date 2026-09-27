"""clarification_states 增加 test_points 列：两阶段生成的功能点清单草稿，供用户在生成前确认/修改。

Revision ID: 0010_test_points
Revises: 0009_system_settings
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0010_test_points"
down_revision: Union[str, None] = "0009_system_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE clarification_states ADD COLUMN IF NOT EXISTS test_points JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE clarification_states DROP COLUMN IF EXISTS test_points")
