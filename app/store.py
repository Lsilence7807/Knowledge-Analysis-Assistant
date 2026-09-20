# 文件：app/store.py
# 作用：SQLite 元数据层：建表与读写，所有元数据只经这里落盘
# 阶段：P0 骨架与契约冻结（扩展阶段在此追加新表）
# 依赖：标准库 sqlite3、app/config.py
from __future__ import annotations

import sqlite3
from contextlib import closing

from app import config

DDL: tuple[str, ...] = (
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
)


def connect() -> sqlite3.Connection:
    """打开元数据库连接，调用方负责关闭。"""
    config.ensure_dirs()
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables() -> None:
    """建表，幂等；扩展阶段新增表时在这里追加一条 DDL。"""
    with closing(connect()) as conn:
        for stmt in DDL:
            conn.execute(stmt)
        conn.commit()


def table_names() -> list[str]:
    """当前库里的表名，供自检与测试使用。"""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    return [row["name"] for row in rows]
