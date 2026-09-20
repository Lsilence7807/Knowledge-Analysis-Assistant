# 文件：app/db.py
# 作用：DuckDB 的唯一出口：登记数据集表、执行查询 SQL（守卫/超时/行数上限）、描述统计与异常检测
# 阶段：P0 骨架与契约冻结（守卫、超时、行数上限与统计在 P2 补全；A 类补丁加 drop_table）
# 依赖：duckdb、pandas、threading、app/config.py
from __future__ import annotations

import threading

import duckdb
import pandas as pd

from app import config

DEFAULT_LIMIT = 5000
DEFAULT_TIMEOUT_S = 10


class SQLRejected(Exception):
    """SQL 未通过守卫或执行失败；错误信息可直接回给用户。"""


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


def drop_table(dataset_id: str) -> None:
    """删掉数据集在 DuckDB 里的表（K-015，幂等）；查询路径永远不碰这条写连接。"""
    with duckdb.connect(str(config.DUCKDB_PATH)) as conn:
        conn.execute(f'DROP TABLE IF EXISTS "{table_name(dataset_id)}"')


def exec_sql(sql: str, limit: int = DEFAULT_LIMIT, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
    """执行单条只读 SELECT，返回 {"sql","columns","rows","row_count","truncated","hint"}。"""
    text = _guard(sql)
    conn = connect(read_only=True)
    state = {"timeout": False}

    def abort() -> None:
        state["timeout"] = True
        conn.interrupt()

    # 超时靠定时器调 interrupt() 打断，比另起进程/线程池简单，也留住了只读连接的语义
    timer = threading.Timer(timeout_s, abort)
    timer.start()
    try:
        cursor = conn.execute(text)
        columns = [item[0] for item in cursor.description or []]
        # 多取一行只为判断是否被截断，不在内存里放整表
        fetched = cursor.fetchmany(limit + 1)
    except duckdb.Error as exc:
        if state["timeout"]:
            raise SQLRejected(f"查询超过 {timeout_s}s 已中断，请缩小范围后重试") from exc
        raise SQLRejected(f"查询失败：{exc}") from exc
    finally:
        timer.cancel()
        conn.close()
    truncated = len(fetched) > limit
    rows = [list(row) for row in fetched[:limit]]
    return {
        "sql": text,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "hint": f"结果超过 {limit} 行，只返回前 {limit} 行" if truncated else None,
    }


def _guard(sql: str) -> str:
    """校验 SQL 只含单条只读 SELECT；解析用 DuckDB 自己的解析器，避免正则漏判。"""
    text = (sql or "").strip()
    if not text:
        raise SQLRejected("SQL 不能为空")
    # 注释能藏起第二条语句，整条拒掉比逐段识别注释边界更不容易漏；字符串里出现 -- 或 ; 也一并拒
    if "--" in text or "/*" in text:
        raise SQLRejected("SQL 里不允许写注释")
    if ";" in text:
        raise SQLRejected("只允许单条 SELECT，不要写分号")
    try:
        statements = duckdb.extract_statements(text)
    except duckdb.Error as exc:
        raise SQLRejected(f"SQL 解析失败：{exc}") from exc
    if len(statements) != 1:
        raise SQLRejected("只允许单条 SELECT")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise SQLRejected("只允许 SELECT 查询，不允许建表、写入或删除")
    return text


def describe(dataset_id: str, columns: list[str] | None = None) -> dict:
    """返回数据集的描述统计与 IQR / z-score 异常行。"""
    name = table_name(dataset_id)
    result = exec_sql(f'SELECT * FROM "{name}"')
    frame = pd.DataFrame(result["rows"], columns=result["columns"])
    if columns:
        frame = frame[[col for col in columns if col in frame.columns]]
    stats: list[dict] = []
    anomalies: list[dict] = []
    for column in frame.columns:
        series = frame[column]
        item: dict = {
            "name": column,
            "dtype": str(series.dtype),
            "count": int(series.notna().sum()),
            "missing": int(series.isna().sum()),
        }
        if pd.api.types.is_numeric_dtype(series) and series.notna().any():
            item.update({
                "min": _number(series.min()),
                "q25": _number(series.quantile(0.25)),
                "median": _number(series.median()),
                "q75": _number(series.quantile(0.75)),
                "max": _number(series.max()),
                "mean": _number(series.mean()),
                "std": _number(series.std()),
            })
            anomalies.extend(_outliers(column, series))
        stats.append(item)
    return {
        "table": name,
        "row_count": len(frame),
        "truncated": result["truncated"],
        "columns": stats,
        "anomalies": anomalies,
    }


def _outliers(column: str, series) -> list[dict]:
    """按 IQR 与 z-score 两个口径标出异常行；两个口径都命中时 reason 会写全。"""
    values = series.dropna()
    if len(values) < 4:
        return []
    q1, q3 = values.quantile(0.25), values.quantile(0.75)
    iqr = q3 - q1
    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    mean, std = values.mean(), values.std()
    hits: list[dict] = []
    for index, value in values.items():
        reasons: list[str] = []
        if value < low:
            reasons.append(f"低于 IQR 下界 {round(float(low), 4)}")
        elif value > high:
            reasons.append(f"高于 IQR 上界 {round(float(high), 4)}")
        if std and std > 0 and abs(value - mean) / std > 3:
            reasons.append("z-score 超过 3")
        if reasons:
            hits.append({
                "column": column,
                "row_hint": f"第 {index + 1} 行",
                "value": _number(value),
                "reason": "；".join(reasons),
            })
    return hits


def _number(value):
    """numpy / pandas 标量转成 JSON 友好的 Python 数字，空值转 None。"""
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return int(number) if number.is_integer() else number
