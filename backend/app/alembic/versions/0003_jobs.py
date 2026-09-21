# 文件：backend/app/alembic/versions/0003_jobs.py
# 作用：F9 新增 jobs 表（§4.4）：后台作业的类型、入参、状态、进度、结果与两个时间戳
# 阶段：F9 作业与文档（兼 P18）
# 依赖：alembic
from __future__ import annotations

from alembic import op

revision = "0003_jobs"
down_revision = "0002_eval_runs"
branch_labels = None
depends_on = None

# 带 IF NOT EXISTS：老库与新库都能直接 upgrade
DDL = """CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT, payload_json TEXT, status TEXT, progress REAL,
    result_json TEXT, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"""


def upgrade() -> None:
    """只加这一张表，不动既有表与数据。"""
    op.execute(DDL)


def downgrade() -> None:
    """回滚删表：排队中的作业会一起没，确认没人再读再降。"""
    op.execute("DROP TABLE IF EXISTS jobs")
