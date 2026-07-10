"""进化闭环：feedbacks 加分诊列 + 新增 feedback_consumptions 消费台账。

Revision ID: 0005_feedback_triage
Revises: 0004_prompt_suggestions
Create Date: 2026-07-06

负反馈统一分诊 + 消费台账（方案 3 第一步）：
  - feedbacks 加 reason（dislike 可选原因）/ triage（归一 intent）/ triage_targets（出口列表）
  - feedback_consumptions：记录反馈被哪个出口消费，三出口据此只吃未消费增量

fresh DB（init_db + create_all）已按新模型建表，IF NOT EXISTS 保证 no-op；
只有已 stamp baseline 的老库才真正 ADD/CREATE。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0005_feedback_triage"
down_revision: Union[str, None] = "0004_prompt_suggestions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS reason TEXT")
    op.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS triage VARCHAR(20)")
    op.execute("ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS triage_targets VARCHAR(64)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_consumptions (
            id SERIAL PRIMARY KEY,
            feedback_id INTEGER NOT NULL REFERENCES feedbacks(id) ON DELETE CASCADE,
            output_kind VARCHAR(20) NOT NULL,
            output_ref_id INTEGER,
            created_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_feedback_consumption UNIQUE (feedback_id, output_kind)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_feedback_consumptions_feedback_id "
        "ON feedback_consumptions (feedback_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feedback_consumptions")
    op.execute("ALTER TABLE feedbacks DROP COLUMN IF EXISTS triage_targets")
    op.execute("ALTER TABLE feedbacks DROP COLUMN IF EXISTS triage")
    op.execute("ALTER TABLE feedbacks DROP COLUMN IF EXISTS reason")
