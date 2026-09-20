# 文件：backend/app/services/agent.py
# 作用：Agent 图（LangGraph）：规划节点 → 工具节点 → 再规划；产出 /ask 的响应并把步骤落 agent_steps
# 阶段：F4 Agent 与记忆换 LangGraph（兼 P13；K-008 的截断口径、K-012/K-034 的上下文都在这里收口）
# 依赖：hashlib、json、time、uuid、dataclasses、typing、langgraph、backend/app/core/config.py、
#       backend/app/services/{llm,memory,store,tools}.py
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.core import config
from app.services import llm, memory, store, tools

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


class State(TypedDict, total=False):
    """图状态：nodes 每轮回整份 messages/steps（后写覆盖先写），checkpointer 存的就永远是当前态。

    覆盖而不是追加是有意的：带 checkpointer 的图会把上一轮的状态并回来，追加会把同会话
    第二次提问的历史消息重新喂给模型（工具结果、旧表格），P13 的步数与截断断言都会跟着变。
    """

    messages: list
    payloads: list
    allow: list
    budget: int
    model_id: str | None
    dataset_id: str
    steps: list
    table: dict | None
    degraded: list
    message: str
    answered: bool
    pending: list


def run(
    question: str,
    session_id: str | None,
    dataset_id: str,
    model_id: str | None = None,
    max_steps: int | None = None,
) -> AgentResult:
    """跑一轮 agent 图；模型不可用、步数耗尽、工具失败都不抛错，降级信息写在返回值里。"""
    cfg = tools.settings()
    budget = max(1, min(int(max_steps or config.AGENT_MAX_STEPS), int(cfg.get("max_steps", 6))))
    allow = [str(name) for name in cfg.get("allow") or []]
    task_id = f"t_{uuid4().hex[:8]}"
    system = f"{SYSTEM.format(max_steps=budget)}\n\n{_schema_hint(dataset_id)}"
    extra = memory.agent_context(question, session_id)
    if extra:
        system = f"{system}\n\n{extra}"
    state: State = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": question}],
        "payloads": tools.all_tools(set(allow)),
        "allow": allow,
        "budget": budget,
        "model_id": model_id,
        "dataset_id": dataset_id,
        "steps": [],
        "table": None,
        "degraded": [],
        "message": "",
        "answered": False,
        "pending": [],
    }
    final = graph(budget).invoke(
        state,
        {
            "configurable": {"thread_id": memory.thread_id(session_id, task_id)},
            # 每轮规划 + 一轮工具 = 2 个 super-step，留 2 步收尾
            "recursion_limit": budget * 2 + 2,
        },
    )
    result = AgentResult(
        task_id=task_id,
        dataset_id=dataset_id,
        steps=list(final.get("steps") or []),
        degraded=list(final.get("degraded") or []),
        message=str(final.get("message") or ""),
    )
    if not final.get("answered") and not result.degraded:
        result.degraded.append("agent:max_steps")
        result.message = f"步数预算（{budget} 步）已用尽，返回已完成的步骤与阶段性结果"
    table = final.get("table")
    if table:
        result.sql, result.columns = table["sql"], table["columns"]
        result.rows, result.row_count = table["rows"], table["row_count"]
        result.truncated = table["truncated"]
    # 步骤流水与响应里的 steps 一一对应，P10 的读路由直接查这张表
    for step in result.steps:
        store.insert_agent_step(result.task_id, step)
    return result


def graph(budget: int):
    """编译 agent 图：规划 →（有条件）工具 → 规划 …；会话打开时挂 SqliteSaver 存状态。"""
    builder = StateGraph(State)
    builder.add_node("plan", _plan)
    builder.add_node("tools", _tools)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", _after_plan, {"tools": "tools", "end": END})
    builder.add_conditional_edges("tools", _after_tools, {"plan": "plan", "end": END})
    checkpointer = memory.checkpointer() if config.ENABLE_MEMORY else None
    return builder.compile(checkpointer=checkpointer)


def _plan(state: State) -> dict:
    """规划节点：模型决定下一步调哪些工具，或直接给结论。

    上下文（轮次、知识库片段、技能、指标口径）已经在系统提示里给全了，这里不再补查。
    """
    try:
        reply = llm.chat_tools(state["messages"], state["payloads"], state.get("model_id"))
    except (llm.LLMUnavailable, llm.LLMError) as exc:
        # 第一步就失败说明模型这条路整体不可用，与 P3 用同一个降级标识；用户仍可手写 /query
        return {
            "degraded": ["query:llm"] if not state["steps"] else ["agent:llm"],
            "message": str(exc),
            "answered": False,
            "pending": [],
        }
    calls = reply.get("tool_calls") or []
    if not calls:
        return {
            "message": str(reply.get("content") or "").strip() or "模型没有给出结论",
            "answered": True,
            "pending": [],
        }
    return {"messages": [*state["messages"], _assistant_message(reply)], "pending": calls}


def _tools(state: State) -> dict:
    """工具节点：按预算执行本轮的每个工具调用，把步骤与结果记回状态。"""
    steps = list(state["steps"])
    messages = list(state["messages"])
    table = state.get("table")
    for tool_call in state.get("pending") or []:
        if len(steps) >= int(state["budget"]):
            break  # 预算已满：这一步不执行，由收尾信息说明
        step, payload, table_payload = _run_step(tool_call, state["allow"], state["dataset_id"], len(steps) + 1)
        steps.append(step)
        messages.append(_tool_message(tool_call.get("id"), payload))
        table = table_payload or table
    return {"steps": steps, "messages": messages, "table": table, "pending": []}


def _after_plan(state: State) -> str:
    """规划完去哪：还有工具要调就进工具节点，否则收尾（模型挂了或步数用完也算收尾）。"""
    if state.get("answered") or state.get("degraded"):
        return "end"
    if not state.get("pending"):
        return "end"
    return "tools" if len(state["steps"]) < int(state["budget"]) else "end"


def _after_tools(state: State) -> str:
    """工具跑完回到规划节点；预算用完就收尾。"""
    return "end" if len(state["steps"]) >= int(state["budget"]) else "plan"


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
