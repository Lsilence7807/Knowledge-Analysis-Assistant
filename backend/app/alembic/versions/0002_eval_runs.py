# 文件：backend/app/alembic/versions/0002_eval_runs.py
# 作用：F8 新增 eval_runs 表（§4.4）：每轮评测的总数、通过率、fidelity、降级率、成本与基线差值
# 阶段：F8 观测与评测（兼 P16）
# 依赖：alembic
from __future__ import annotations

from alembic import op

revision = "0002_eval_runs"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# 带 IF NOT EXISTS：老库（已跑过基线）与新库都能直接 upgrade
DDL = """CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, golden_path TEXT, total INTEGER,
    sql_pass_rate REAL, fidelity_rate REAL, degrade_rate REAL, cost_total REAL,
    baseline_delta_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""


def upgrade() -> None:
    """只加这一张表，不动既有表与数据。"""
    op.execute(DDL)


def downgrade() -> None:
    """回滚删表：评测历史会一起没，确认没人再读再降。"""
    op.execute("DROP TABLE IF EXISTS eval_runs")
