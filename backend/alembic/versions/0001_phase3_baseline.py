"""Phase 3 baseline: snapshot of the schema produced by app.database.init_db().

Revision ID: 0001_phase3_baseline
Revises:
Create Date: 2026-06-04 07:49:31

为什么是 baseline 而不是从空库创建：
- Phase 1/2 时项目用的是 SQLAlchemy create_all + ad-hoc ALTER（见 app/database.py:init_db）
- Phase 3 切到 Alembic 但保留 init_db() 兜底，以免现有部署需要手动迁移
- 这条 baseline 描述"init_db 跑完后应当达到的 state"——已经在该 state 的库执行 `alembic stamp head` 即可对齐
- 后续新增字段都走新的 migration，init_db() 不再加新 ALTER

执行顺序（首次启用 Alembic 的部署）：
  1. 服务启动一次让 init_db() 把 schema 拉齐（兼容老路径）
  2. `alembic stamp head` 标记当前 revision = 0001_phase3_baseline
  3. 之后任何 schema 改动都在 alembic/versions/ 里加新文件
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_phase3_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: 表已由 init_db() 在 Phase 3 上线时建好。

    给"完全空库 + 只跑 alembic upgrade"的场景补全 schema 是后续 0002 才做的事；
    本 revision 的目的是为新加 migration 提供锚点。"""
    pass


def downgrade() -> None:
    """No structured downgrade — Phase 3 schema 与 Phase 1/2 不向前兼容（多了
    project_id / pgvector 列类型 / 新表）。如果真要回退 Phase 2，请重建数据库。"""
    pass
