# 文件：backend/app/alembic/versions/0001_baseline.py
# 作用：基线迁移：把 §4.4 现有的表一次性建齐（DDL 与改造前的 store.DDL 逐字一致，一行没改）
# 阶段：F6 数据层换 SQLAlchemy + Alembic
# 依赖：alembic
from __future__ import annotations

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

# 全部带 IF NOT EXISTS：老库（改造前就建过表）跑 upgrade 时不会被已存在的表卡住
TABLES = (
    """CREATE TABLE IF NOT EXISTS datasets (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, source_id TEXT, source_path TEXT,
        table_name TEXT NOT NULL, rows INTEGER, cols INTEGER, profile_json TEXT,
        clean_log TEXT, table_version INTEGER DEFAULT 1, owner TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, dataset_id TEXT, session_id TEXT, kind TEXT, question TEXT,
        sql TEXT, status TEXT, degraded_json TEXT, error TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS agent_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, n INTEGER, tool TEXT,
        args_json TEXT, ok INTEGER, ms INTEGER, rows INTEGER, error TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS trace_spans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, name TEXT, model_id TEXT,
        tokens_in INTEGER, tokens_out INTEGER, cost REAL, ms INTEGER, ok INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS capability_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, capability TEXT, event TEXT, detail TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY, slug TEXT UNIQUE, path TEXT, description TEXT,
        kind TEXT, enabled INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS kb_docs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, title TEXT, chunk_no INTEGER,
        content TEXT, source_type TEXT, vector BLOB, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    # 检索索引与 kb_docs 分开存：trigram 分词是按子串命中中文的唯一选择（unicode61 搜不到连续中文）
    """CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(content, tokenize='trigram')""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, dataset_id TEXT, title TEXT, model_id TEXT, owner TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_active_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, seq INTEGER,
        question TEXT, sql TEXT, columns_json TEXT, insight_summary TEXT, cached INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS qa_cache (
        key TEXT PRIMARY KEY, dataset_id TEXT, table_version INTEGER, norm_question TEXT, model_id TEXT,
        sql TEXT, insight_json TEXT, vector BLOB, hits INTEGER DEFAULT 0,
        last_hit_at TEXT DEFAULT CURRENT_TIMESTAMP, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
)


def upgrade() -> None:
    """建齐现有表；后续阶段加表就再加一个 revision，不改这一版。"""
    for statement in TABLES:
        op.execute(statement)


def downgrade() -> None:
    """回退到空库（删表，含 FTS 虚表）。"""
    dropped = (
        "qa_cache",
        "turns",
        "sessions",
        "kb_fts",
        "kb_docs",
        "skills",
        "capability_log",
        "trace_spans",
        "agent_steps",
        "tasks",
        "datasets",
    )
    for name in dropped:
        op.execute(f"DROP TABLE IF EXISTS {name}")
