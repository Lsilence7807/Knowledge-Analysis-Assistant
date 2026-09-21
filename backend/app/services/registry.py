# 文件：backend/app/services/registry.py
# 作用：能力注册表：新能力的唯一接入点，provider 只在这里被惰性装配
# 阶段：P0 骨架与契约冻结（P2 注册 stats；P3 注册 query、llm；P13 注册 agent；P6 注册 skills；P7 注册 kb；
#       P15 注册 sandbox；P8 注册 mcp；F9 注册 jobs）
# 依赖：标准库 logging、backend/app/core/config.py
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.core import config

logger = logging.getLogger(__name__)

CAPABILITIES: dict[str, str] = {
    "query": "自然语言转 SQL（P3）",
    "stats": "描述统计与异常检测（P2）",
    "agent": "多步工具调用循环（P13）",
    "insight": "结构化结论生成（P4）",
    "llm": "模型可用（缺密钥时为 false）",
    "skills": "技能库：导入 SKILL.md 并按需取用（P6）",
    "kb": "知识库：md/txt 导入与关键词检索（P7）",
    "sandbox": "受限代码执行：隔离子进程跑 pandas 代码（P15）",
    "mcp": "MCP 插件：外部工具发现与白名单调用（P8）",
    "memory": "会话与上下文：最近轮次 + 知识库/技能/口径一次给全（P10）",
    "jobs": "后台作业：长分析进队列、看进度与结果（P18，F9 落地）",
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
    # 能力工厂可能因缺依赖或配置错抛任意异常，P0 契约要求这里降级成 None，不把错抛给调用方
    except Exception as exc:  # noqa: BLE001
        logger.warning("能力 %s 不可用：%s", name, exc)
        return None


def status() -> dict[str, bool]:
    """能力可用性，供 /capabilities 使用。"""
    return {name: get(name) is not None for name in CAPABILITIES}


def _stats_capability():
    """stats 能力：描述统计走 DuckDB，不依赖模型；K-022 补注册，此前只声明没装配。"""
    from app.core import db

    return db.describe


register("stats", _stats_capability)


def _query_capability():
    """query 能力：有密钥且能取到模型 profile 才算可用，否则 get() 兜成不可用。"""
    from app.services import llm, nlu

    llm.ensure_ready()
    return nlu.to_sql


def _llm_capability():
    """llm 能力：等价于「存在可用的模型 profile」。"""
    from app.services import llm

    llm.ensure_ready()
    return llm


register("query", _query_capability)
register("llm", _llm_capability)


def _agent_capability():
    """agent 能力：有密钥才算可用，返回 agent.run。"""
    from app.services import agent, llm

    llm.ensure_ready()
    return agent.run


register("agent", _agent_capability)


def _insight_capability():
    """insight 能力：有密钥才算可用，返回 insight.summarize。"""
    from app.services import insight, llm

    llm.ensure_ready()
    return insight.summarize


register("insight", _insight_capability)


def _skills_capability():
    """skills 能力：ENABLE_SKILLS 打开才可用，返回 providers.skills 模块（工具层从这里取用）。"""
    from app.providers import skills

    return skills if skills.ENABLED else None


register("skills", _skills_capability)


def _kb_capability():
    """kb 能力：ENABLE_KB 打开才可用，返回 providers.kb 模块（路由与工具从这里取用）。

    F9：PDF/Word 的解析后端在 providers/docs.py，由这里注入给 kb——两个 provider 不互相 import。
    """
    from app.providers import docs, kb

    kb.bind_parser(docs.extract if docs.ENABLED else None)
    return kb if kb.ENABLED else None


register("kb", _kb_capability)


def _jobs_capability():
    """jobs 能力：ENABLE_JOBS 打开才可用，返回 services.jobs 模块（队列、进度与定时维护都在里面）。"""
    from app.services import jobs

    return jobs if jobs.enabled() else None


register("jobs", _jobs_capability)


def _sandbox_capability():
    """sandbox 能力：ENABLE_SANDBOX 打开才可用，返回 app.sandbox 模块（路由与工具从这里取用）。"""
    from app.services import sandbox

    return sandbox if sandbox.ENABLED else None


register("sandbox", _sandbox_capability)


def _mcp_capability():
    """mcp 能力：ENABLE_MCP 打开且配置的 server 真能起来（列得出工具）才算可用。

    探测会真起一次子进程，所以默认关闭时 /capabilities 零开销；server 崩了或配置坏了这里自然变 false。
    """
    from app.providers import mcp_client

    if not mcp_client.ENABLED:
        return None
    return mcp_client if mcp_client.available() else None


register("mcp", _mcp_capability)


def _memory_capability():
    """memory 能力：ENABLE_MEMORY 打开才可用，返回 services.memory 模块。"""
    from app.services import memory

    return memory if config.ENABLE_MEMORY else None


register("memory", _memory_capability)
