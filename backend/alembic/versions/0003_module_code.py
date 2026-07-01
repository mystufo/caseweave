"""模块中英文双名：modules 加 code 列（英文名 = 用例编号前缀 case_prefix）。

Revision ID: 0003_module_code
Revises: 0002_prompt_versioning
Create Date: 2026-06-30

模块此前只有中文 name。现补一列 code（大写英文，如 ORDER-MGMT），既作英文名，
也用作该模块下测试用例的编号前缀，让「模块 / 用例模块名 / 编号前缀」三者对齐。

fresh DB（init_db + create_all）已按新模型建表，IF NOT EXISTS 保证对其 no-op；
只有已 stamp baseline 的老库才真正 ADD COLUMN。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0003_module_code"
down_revision: Union[str, None] = "0002_prompt_versioning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE modules ADD COLUMN IF NOT EXISTS code VARCHAR(40)")


def downgrade() -> None:
    op.execute("ALTER TABLE modules DROP COLUMN IF EXISTS code")
