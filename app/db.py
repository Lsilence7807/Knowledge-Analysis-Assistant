# 文件：app/db.py
# 作用：DuckDB 的唯一出口：登记数据集表、执行查询 SQL，SQL 守卫在此
# 阶段：P0 骨架与契约冻结（守卫在 P2 补全：多语句、DDL/DML、超时、行数上限）
# 依赖：duckdb、app/config.py
from __future__ import annotations

import duckdb

from app import config


class SQLRejected(Exception):
    """SQL 未通过守卫。"""


def table_name(dataset_id: str) -> str:
    """数据集对应的 DuckDB 表名。"""
    return f"ds_{dataset_id}"


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接，默认只读。"""
    config.ensure_dirs()
    if read_only and config.DUCKDB_PATH.exists():
        return duckdb.connect(str(config.DUCKDB_PATH), read_only=True)
    return duckdb.connect(str(config.DUCKDB_PATH))


def register_table(dataset_id: str, df) -> str:
    """把 DataFrame 落成 DuckDB 表，返回表名。"""
    name = table_name(dataset_id)
    with duckdb.connect(str(config.DUCKDB_PATH)) as conn:
        conn.register("_incoming", df)
        conn.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _incoming')
        conn.unregister("_incoming")
    return name


def exec_sql(sql: str, limit: int = 5000, timeout_s: int = 10) -> dict:
    """执行单条 SELECT，返回 {"columns": [...], "rows": [[...]]}。"""
    _guard(sql)
    with duckdb.connect(str(config.DUCKDB_PATH), read_only=True) as conn:
        cursor = conn.execute(sql)
        columns = [item[0] for item in cursor.description or []]
        rows = cursor.fetchmany(limit)
    return {"columns": columns, "rows": [list(row) for row in rows]}


def _guard(sql: str) -> None:
    """最小拦截；P2 补齐多语句、注释绕过、超时与行数上限。"""
    text = sql.strip().rstrip(";").strip()
    if ";" in text:
        raise SQLRejected("只允许单条语句")
    if not text.lower().startswith(("select", "with")):
        raise SQLRejected("只允许 SELECT 查询")
