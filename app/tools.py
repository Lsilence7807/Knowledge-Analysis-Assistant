# 文件：app/tools.py
# 作用：Agent 可调用工具的注册表与 3 个只读工具（run_sql / describe_stats / detect_anomalies）
# 阶段：P13 Agent 循环与工具调用
# 依赖：json、app/config.py、app/db.py
from __future__ import annotations

import json

from app import config, db

KINDS = ("read", "write")
DEFAULT_SETTINGS: dict = {
    "max_steps": 6,
    "allow": ["run_sql", "describe_stats", "detect_anomalies"],
    "timeouts": {"run_sql": 10, "describe_stats": 15, "detect_anomalies": 15},
    "max_rows_per_step": 200,
}

_TOOLS: dict[str, dict] = {}


class ToolError(RuntimeError):
    """工具调用失败（名字不存在、参数不匹配、执行报错）；信息可直接回给模型。"""


def settings() -> dict:
    """读 config/tools.json（白名单、步数预算、每步超时、单步行数上限）；文件坏了用默认值。"""
    try:
        loaded = json.loads(config.TOOLS_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    # 每次调用都读文件：改完白名单或预算不用重启服务
    return {**DEFAULT_SETTINGS, **loaded} if isinstance(loaded, dict) else dict(DEFAULT_SETTINGS)


def register(name: str, fn, schema: dict, kind: str) -> None:
    """登记一个工具：schema 是参数的 JSON Schema，kind 取 read / write，描述取函数 docstring。"""
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {KINDS}，收到 {kind}")
    _TOOLS[name] = {
        "fn": fn,
        "schema": schema,
        "kind": kind,
        "description": (fn.__doc__ or "").strip(),
    }


def all_tools(allow: set[str] | None = None) -> list[dict]:
    """按 OpenAI tools 格式列出工具；给了 allow 就只留白名单里的。"""
    names = sorted(_TOOLS if allow is None else set(_TOOLS) & set(allow))
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _TOOLS[name]["description"],
                "parameters": _TOOLS[name]["schema"],
            },
        }
        for name in names
    ]


def kinds() -> dict[str, str]:
    """已注册工具的名字 → 读写类型，供 /tools 与排障查看。"""
    return {name: tool["kind"] for name, tool in _TOOLS.items()}


def call(name: str, arguments: dict) -> dict:
    """执行一个工具并返回结果；名字不存在或执行失败抛 ToolError（含 SQL 被守卫拒绝）。"""
    tool = _TOOLS.get(name)
    if tool is None:
        raise ToolError(f"没有这个工具：{name}")
    try:
        return tool["fn"](**(arguments if isinstance(arguments, dict) else {}))
    except TypeError as exc:
        # 参数名或类型不对：让模型下一轮自己改，不当成系统故障
        raise ToolError(f"工具 {name} 参数不匹配：{exc}") from exc
    except db.SQLRejected as exc:
        raise ToolError(f"工具 {name} 执行失败：{exc}") from exc
    except Exception as exc:
        raise ToolError(f"工具 {name} 执行失败：{exc}") from exc


COLUMNS_ARG = {
    "type": "object",
    "properties": {
        "columns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "只处理这几列，省略则全部",
        }
    },
}


def _run_sql(sql: str = "", dataset_id: str = "") -> dict:
    """执行单条只读 SELECT（不带分号、不做写操作），返回列名与最多 200 行结果。"""
    # dataset_id 由 agent 统一注入，这里用不上：守卫已把语句限死在只读 SELECT，表名由模型自己写
    cfg = settings()
    rows = int(cfg.get("max_rows_per_step", 200))
    timeout = int((cfg.get("timeouts") or {}).get("run_sql", 10))
    return db.exec_sql(sql, limit=rows, timeout_s=timeout)


def _describe_stats(columns: list | None = None, dataset_id: str = "") -> dict:
    """返回数据集的描述统计：每列的计数、缺失、分位数、均值与标准差。"""
    return db.describe(dataset_id, columns)


def _detect_anomalies(columns: list | None = None, dataset_id: str = "") -> dict:
    """返回数据集里被 IQR 或 z-score 判为异常的行及原因。"""
    result = db.describe(dataset_id, columns)
    return {"table": result["table"], "row_count": result["row_count"], "anomalies": result["anomalies"]}


register(
    "run_sql",
    _run_sql,
    {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "单条只读 SELECT，不带分号"}},
        "required": ["sql"],
    },
    "read",
)
register("describe_stats", _describe_stats, COLUMNS_ARG, "read")
register("detect_anomalies", _detect_anomalies, COLUMNS_ARG, "read")
