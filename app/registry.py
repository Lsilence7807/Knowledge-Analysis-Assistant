# 文件：app/registry.py
# 作用：能力注册表：新能力的唯一接入点，provider 只在这里被惰性装配
# 阶段：P0 骨架与契约冻结（P3 注册 query、llm；P13 注册 agent）
# 依赖：标准库 logging
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

CAPABILITIES: dict[str, str] = {
    "query": "自然语言转 SQL（P3）",
    "stats": "描述统计与异常检测（P2）",
    "agent": "多步工具调用循环（P13）",
    "insight": "结构化结论生成（P4）",
    "llm": "模型可用（缺密钥时为 false）",
}

_FACTORIES: dict[str, Callable[[], Any]] = {}


def register(name: str, factory: Callable[[], Any]) -> None:
    """登记能力工厂；名字必须在 CAPABILITIES 里声明过。"""
    if name not in CAPABILITIES:
        raise KeyError(f"未声明的能力：{name}")
    _FACTORIES[name] = factory


def get(name: str) -> Any:
    """取能力实例；不可用一律返回 None，调用方必须降级而不是抛错。"""
    factory = _FACTORIES.get(name)
    if factory is None:
        return None
    try:
        return factory()
    except Exception as exc:
        logger.warning("能力 %s 不可用：%s", name, exc)
        return None


def status() -> dict[str, bool]:
    """能力可用性，供 /capabilities 使用。"""
    return {name: get(name) is not None for name in CAPABILITIES}


def _query_capability():
    """query 能力：有密钥且能取到模型 profile 才算可用，否则 get() 兜成不可用。"""
    from app import llm, nlu

    llm.ensure_ready()
    return nlu.to_sql


def _llm_capability():
    """llm 能力：等价于「存在可用的模型 profile」。"""
    from app import llm

    llm.ensure_ready()
    return llm


register("query", _query_capability)
register("llm", _llm_capability)


def _agent_capability():
    """agent 能力：有密钥才算可用，返回 agent.run。"""
    from app import agent, llm

    llm.ensure_ready()
    return agent.run


register("agent", _agent_capability)


def _insight_capability():
    """insight 能力：有密钥才算可用，返回 insight.summarize。"""
    from app import insight, llm

    llm.ensure_ready()
    return insight.summarize


register("insight", _insight_capability)
