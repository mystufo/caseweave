"""Phase 4.2: prompt_versions 加 project_id / created_by（人工 Prompt 版本化）。

Revision ID: 0002_prompt_versioning
Revises: 0001_phase3_baseline
Create Date: 2026-06-29

prompt_versions 表在 Phase 1 就随 init_db() 建好了，但一直没人写。Phase 4.2 把
3 个硬编码 system prompt 迁进来做项目级版本化，需要补两列：
  - project_id：按项目隔离（每个项目可定制自己的 clarifier/generator 提示词）
  - created_by：记录是哪个用户保存的版本

fresh DB（init_db + create_all）已经按新模型建表，本迁移用 IF NOT EXISTS 保证
对这类库是 no-op；只有"已 stamp baseline 的老库"才真正 ADD COLUMN。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0002_prompt_versioning"
down_revision: Union[str, None] = "0001_phase3_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS project_id INTEGER")
    op.execute("ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS created_by INTEGER")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_prompt_versions_project_id "
        "ON prompt_versions (project_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_prompt_versions_project_id")
    op.execute("ALTER TABLE prompt_versions DROP COLUMN IF EXISTS created_by")
    op.execute("ALTER TABLE prompt_versions DROP COLUMN IF EXISTS project_id")
