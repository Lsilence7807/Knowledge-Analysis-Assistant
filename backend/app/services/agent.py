# 文件：backend/app/services/agent.py
# 作用：Agent 循环：模型规划 → 调工具 → 观察 → 再规划；产出 /ask 的响应并把步骤落 agent_steps
# 阶段：P13 Agent 循环与工具调用（K-008：回模型的单步结果按 200 行截，响应结果表按 /query 口径给足）
# 依赖：hashlib、json、time、uuid、dataclasses、backend/app/core/config.py、
#       backend/app/services/llm.py、backend/app/services/store.py、backend/app/services/tools.py
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from uuid import uuid4

from app.core import config
from app.services import llm, store, tools

SYSTEM = """你是数据分析助手，用工具回答用户关于一个数据集的问题。
规则：
1. 只用给出的工具拿数据，不要凭空推断数值。
2. 一次只做一件事，看到结果再决定下一步。
3. 数据够了就停止调用工具，用中文说清结论与依据；查不到的直接说明。
4. 最多 {max_steps} 步，超出预算会带着已完成的结果收尾。"""


@dataclass
class AgentResult:
    """一次提问的结果，字段与 /ask 响应一一对应。"""

    task_id: str
    dataset_id: str
    sql: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    steps: list = field(default_factory=list)
    cached: bool = False
    reused_sql: bool = False
    degraded: list = field(default_factory=list)
    message: str = ""


def run(
    question: str,
    session_id: str | None,
    dataset_id: str,
    model_id: str | None = None,
    max_steps: int | None = None,
) -> AgentResult:
    """跑一轮 agent 循环；模型不可用、步数耗尽、工具失败都不抛错，降级信息写在返回值里。"""
    cfg = tools.settings()
    budget = max(1, min(int(max_steps or config.AGENT_MAX_STEPS), int(cfg.get("max_steps", 6))))
    allow = [str(name) for name in cfg.get("allow") or []]
    payloads = tools.all_tools(set(allow))
    result = AgentResult(task_id=f"t_{uuid4().hex[:8]}", dataset_id=dataset_id)
    messages = [
        {"role": "system", "content": f"{SYSTEM.format(max_steps=budget)}\n\n{_schema_hint(dataset_id)}"},
        {"role": "user", "content": question},
    ]
    table: dict | None = None
    answered = False
    while len(result.steps) < budget:
        try:
            reply = llm.chat_tools(messages, payloads, model_id)
        except (llm.LLMUnavailable, llm.LLMError) as exc:
            # 第一步就失败说明模型这条路整体不可用，与 P3 用同一个降级标识；用户仍可手写 /query
            result.degraded = ["query:llm"] if not result.steps else ["agent:llm"]
            result.message = str(exc)
            break
        if not reply.get("tool_calls"):
            result.message = str(reply.get("content") or "").strip() or "模型没有给出结论"
            answered = True
            break
        messages.append(_assistant_message(reply))
        for tool_call in reply["tool_calls"]:
            if len(result.steps) >= budget:
                break  # 预算已满：这一步不执行，由循环外的收尾信息说明
            step, payload, table_payload = _run_step(tool_call, allow, dataset_id, len(result.steps) + 1)
            result.steps.append(step)
            messages.append(_tool_message(tool_call.get("id"), payload))
            table = table_payload or table
    if not answered and not result.degraded:
        result.degraded.append("agent:max_steps")
        result.message = f"步数预算（{budget} 步）已用尽，返回已完成的步骤与阶段性结果"
    if table:
        result.sql, result.columns = table["sql"], table["columns"]
        result.rows, result.row_count = table["rows"], table["row_count"]
        result.truncated = table["truncated"]
    # 步骤流水与响应里的 steps 一一对应，P10 的读路由直接查这张表
    for step in result.steps:
        store.insert_agent_step(result.task_id, step)
    return result


def _run_step(tool_call: dict, allow: list[str], dataset_id: str, n: int) -> tuple[dict, dict, dict | None]:
    """执行一步工具，返回（步骤摘要、回给模型的结果、可当最终表用的结果）。"""
    name = str(tool_call.get("name") or "")
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    started = time.perf_counter()
    if name not in allow:
        # 拒绝分两种：已注册但不在白名单算越权（要记账），模型编出来的名字只回可用说明
        if name in tools.kinds():
            detail = f"工具 {name} 不在白名单内，已拒绝；可用工具：{'、'.join(allow)}"
            store.log_capability("agent", "error", f"越权工具调用：{name}")
        else:
            detail = f"没有这个工具：{name}；可用工具：{'、'.join(allow)}"
        payload: dict = {"ok": False, "error": detail}
    else:
        try:
            payload = {"ok": True, **tools.call(name, {**arguments, "dataset_id": dataset_id})}
        except tools.ToolError as exc:
            payload = {"ok": False, "error": str(exc)}
    step = {
        "n": n,
        "tool": name,
        "args_digest": _digest(arguments),
        "ok": payload["ok"],
        "ms": int((time.perf_counter() - started) * 1000),
        "rows": int(payload.get("row_count") or 0),
    }
    if not payload["ok"]:
        step["error"] = payload["error"]
    table = payload if name == "run_sql" and payload["ok"] else None
    return step, _shrink(payload, tools.settings()), table


def _shrink(payload: dict, cfg: dict) -> dict:
    """单步结果行数超过 max_rows_per_step 时只回列名与行数，别让整表撑爆模型上下文。

    全表仍在服务端，模型下一步可以用 run_sql 加聚合或 LIMIT 再取；
    响应里的结果表不走这条限制（K-008：按 /query 同口径最多 5000 行）。
    """
    rows = payload.get("rows")
    limit = int(cfg.get("max_rows_per_step", 200))
    if payload.get("ok") and isinstance(rows, list) and len(rows) > limit:
        kept = {key: value for key, value in payload.items() if key != "rows"}
        kept["hint"] = f"结果超过 {limit} 行，只回列名与行数；要明细请用 run_sql 加聚合或 LIMIT"
        return kept
    return payload


def _assistant_message(reply: dict) -> dict:
    """把 chat_tools 的返回转回 OpenAI 消息格式，下一轮才好带上工具调用记录。"""
    return {
        "role": "assistant",
        "content": reply.get("content") or "",
        "tool_calls": [
            {
                "id": call.get("id") or "call_0",
                "type": "function",
                "function": {
                    "name": call.get("name"),
                    "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                },
            }
            for call in reply["tool_calls"]
        ],
    }


def _tool_message(call_id, payload: dict) -> dict:
    """工具结果回给模型的那条消息。"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _digest(arguments: dict) -> str:
    """参数摘要（4 位十六进制）：步骤列表里不带完整参数，够区分调用即可。"""
    flat = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()[:4]


def _schema_hint(dataset_id: str) -> str:
    """把表名与列名放进系统提示，让模型第一步就能写对 SQL。"""
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        return ""
    columns = "、".join(str(item.get("name")) for item in (dataset["profile"].get("columns") or []))
    return f"表名：{dataset['table_name']}（{dataset['rows']} 行）\n列：{columns}"
