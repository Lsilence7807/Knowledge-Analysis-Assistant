# 文件：backend/app/services/trace.py
# 作用：调用台账：本地 trace_spans 表保底，配了 Langfuse 密钥再往云端发一份；stats 回答「花了多少钱、慢在哪」
# 阶段：F8 观测与评测（兼 P16；ADR-17 废止后 Langfuse 为主、本地表降级）
# 依赖：标准库 contextvars、logging、time；app/core/config.py、app/services/store.py；langfuse（可选）
from __future__ import annotations

import contextvars
import logging
import time

from app.core import config
from app.services import store

logger = logging.getLogger(__name__)

# 当前任务 id：llm 与 tools 的签名是冻结契约，打点靠这个上下文取任务号，不给自己加参数
_TASK: contextvars.ContextVar[str] = contextvars.ContextVar("trace_task_id", default="")
_CLIENT = None


def bind(task_id: str) -> None:
    """给当前请求/任务绑定 task_id（路由与 agent 各调一次）。"""
    _TASK.set(task_id or "")


def current_task() -> str:
    """当前 task_id；没绑过给空串（trace_spans.task_id 允许为空）。"""
    return _TASK.get()


def span(
    task_id: str,
    name: str,
    *,
    model_id: str | None = None,
    tokens: int = 0,
    cost: float = 0.0,
    ms: int = 0,
    ok: bool = True,
) -> None:
    """§4.2 契约里的那一个：只有 token 总数时用它（落在 tokens_in 上）；要分进出用 record()。"""
    record(task_id, name, model_id=model_id, tokens_in=tokens, cost=cost, ms=ms, ok=ok)


def record(
    task_id: str,
    name: str,
    *,
    model_id: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
    ms: int = 0,
    ok: bool = True,
) -> None:
    """记一次调用：本地表必写、Langfuse 可选。观测不许拖垮业务，全程吞异常只记日志。"""
    task = task_id or current_task()
    try:
        store.insert_trace_span(
            task,
            name,
            model_id=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            ms=ms,
            ok=ok,
        )
    except Exception as exc:  # noqa: BLE001 - 打点失败不是业务失败
        logger.warning("trace 落表失败(%s)：%s", name, exc)
    _emit(task, name, model_id=model_id, tokens_in=tokens_in, tokens_out=tokens_out, cost=cost, ms=ms, ok=ok)


def elapsed_ms(started: float) -> int:
    """从 perf_counter 起点算毫秒：llm 与 tools 都要计时，写法只留一份。"""
    return int((time.perf_counter() - started) * 1000)


def usage_of(response) -> tuple[int, int, float]:
    """从 LiteLLM 响应取 (进 token, 出 token, 成本)；取不到给 0（不为记账再抛错）。"""
    usage = getattr(response, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
    hidden = getattr(response, "_hidden_params", None) or {}
    try:
        cost = float(hidden.get("response_cost") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return tokens_in, tokens_out, cost


def stats(days: int = 7) -> dict:
    """最近 days 天的成本与耗时汇总（GET /trace/stats）。"""
    return store.trace_stats(days)


def spans(task_id: str) -> list[dict]:
    """某个任务的调用明细（GET /trace/{task_id}）。"""
    return store.trace_spans(task_id)


def client():
    """Langfuse 客户端：没开观测或缺密钥给 None（纯本地形态）；建一次就复用。"""
    global _CLIENT
    if not config.LANGFUSE_ENABLED or not config.LANGFUSE_PUBLIC_KEY or not config.LANGFUSE_SECRET_KEY:
        return None
    if _CLIENT is None:
        try:
            from langfuse import Langfuse

            _CLIENT = Langfuse(
                public_key=config.LANGFUSE_PUBLIC_KEY,
                secret_key=config.LANGFUSE_SECRET_KEY,
                host=config.LANGFUSE_HOST,
            )
        except Exception as exc:  # noqa: BLE001 - SDK 缺失或初始化失败都退化成纯本地
            logger.warning("Langfuse 初始化失败，转纯本地：%s", exc)
            return None
    return _CLIENT


def _emit(task_id, name, *, model_id, tokens_in, tokens_out, cost, ms, ok) -> None:
    """把同一条 span 也发给 Langfuse；§7：只发长度、耗时、成本这类元数据，不发 prompt 全文与文件内容。"""
    hub = client()
    if hub is None:
        return
    try:
        hub.create_event(
            name=f"span.{name}",
            metadata={
                "task_id": task_id,
                "model_id": model_id,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost": cost,
                "ms": ms,
                "ok": bool(ok),
            },
        )
    except Exception as exc:  # noqa: BLE001 - 上报失败只留日志
        logger.warning("Langfuse 上报失败(%s)：%s", name, exc)
