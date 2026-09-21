# 文件：backend/app/api/routes/ask.py
# 作用：提问端点：POST /ask 单轮提问（agent 或单跳）与 GET /ask/stream 流式提问（帧序见设计 §4.11）
# 阶段：F1 后端骨架（原 app/main.py 的 /ask 与 /ask/stream 原样搬来）
#       test_p17_stream 引用的 _stream_frames 名字保持不变
# 依赖：fastapi、app/api/deps.py、app/api/routes/insight.py、app/schemas/__init__.py、app/services/*
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sse_starlette.sse import EventSourceResponse

from app.api.deps import CurrentUser, get_dataset
from app.api.routes.insight import attach_insight
from app.core import config, db
from app.schemas import AskIn
from app.services import agent, insight, llm, memory, nlu, store, stream

router = APIRouter()


@router.post("/ask")
def ask(payload: AskIn, user: CurrentUser = None) -> dict:
    """提问 →（模型）→ SQL → 结果表 → 结论；模型不可用或 SQL 不合法时降级，HTTP 仍 200。"""
    dataset_id = payload.dataset_id
    question = payload.question.strip()
    session_id = payload.session_id
    dataset = get_dataset(dataset_id, user)
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    body = _ask_body(question, session_id, dataset_id, dataset)
    body = attach_insight(body, question, dataset, memory.insight_context(question, session_id))
    # K-005：Agent 路径原本不写 tasks，提问历史只留在响应里；这里落一行，degraded 也记进去
    degraded = body.get("degraded") or []
    store.insert_task(
        {
            "id": body.get("task_id"),
            "dataset_id": dataset_id,
            "session_id": session_id,
            "kind": "ask",
            "question": question,
            "sql": body.get("sql") or "",
            "degraded": degraded,
            # message 在成功时是模型的收尾说明，只有降级时它才是错误原因，别混进 error 列
            "error": (body.get("message") or "") if degraded else "",
        }
    )
    memory.add_turn(session_id, question, body.get("sql") or "", body.get("columns") or [], body.get("insight"), False)
    return body


def _ask_body(question: str, session_id: str | None, dataset_id: str, dataset: dict) -> dict:
    """提问 →（agent 或单跳）→ 结果表；/ask 与 /ask/stream 共用这一段，两条路口径必须一致。"""
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    if config.ENABLE_AGENT:
        # P13：/ask 走 agent 循环；关掉 ENABLE_AGENT 就落到下面 P3 的单跳通道（调试与降级用）
        return asdict(agent.run(question, session_id or None, dataset_id))
    base = {
        "task_id": f"t_{uuid4().hex[:8]}",
        "dataset_id": dataset_id,
        "sql": "",
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
        "steps": [],
        "cached": False,
        "reused_sql": False,
        "degraded": [],
        "message": "",
    }
    try:
        sql = nlu.to_sql(question, profile, [])
    except (llm.LLMUnavailable, llm.LLMError, nlu.NLUError) as exc:
        # 模型这条路整体降级：手写 SQL 的 /query 不受影响，用户仍能拿到结果
        return {**base, "degraded": ["query:llm"], "message": str(exc)}
    try:
        result = db.exec_sql(sql)
    except db.SQLRejected as exc:
        return {**base, "degraded": ["query:llm"], "message": f"模型给出的 SQL 未通过校验，已拒绝执行：{exc}"}
    return {**base, **{key: result[key] for key in ("sql", "columns", "rows", "row_count", "truncated")}}


# ---- P17 流式提问（帧序见 §4.11）----
STREAM_SYSTEM = """你在解读一张数据分析的结果表，说给用户听。
规则：只用给出的结果表说话；先一句话结论，再给两三条依据，最后说注意事项；不要编造表里没有的数字。"""


@router.get("/ask/stream")
async def ask_stream(
    request: Request,
    question: str = "",
    dataset_id: str = "",
    session_id: str = "",
    user: CurrentUser = None,
) -> EventSourceResponse:
    """流式提问（SSE）；模型不可用或 SQL 不合法只在流里插 degraded 帧，HTTP 仍是 200。"""
    if not stream.ENABLED:
        raise HTTPException(status_code=503, detail="流式能力未启用：设 ENABLE_STREAM=true 再重启服务")
    # 校验发生在开流之前：数据集不存在、问题为空仍然是普通 HTTP 错误，不用从流里猜
    dataset = await run_in_threadpool(get_dataset, dataset_id, user)
    question = question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    return EventSourceResponse(_stream_frames(request, question, session_id or None, dataset_id, dataset))


async def _gone(request: Request) -> bool:
    """客户端是否已断开：Starlette 的标记只能由 is_disconnected 刷新，所以先 await 一次再同步读。"""
    await request.is_disconnected()
    return stream.canceled(request)


def _stream_prompt(question: str, body: dict, profile: dict) -> str:
    """流式结论的提示：问题 + SQL + 结果表前几行；行数上限与 insight 共用，别让两条路看到不同的表。"""
    lines = [
        f"数据集：{profile.get('table', '')}",
        f"问题：{question}",
        f"SQL：{body.get('sql') or '（没有可用的 SQL）'}",
        f"结果表：{len(body.get('rows') or [])} 行，列：{'、'.join(str(name) for name in body.get('columns') or [])}",
    ]
    lines += [json.dumps(row, ensure_ascii=False, default=str) for row in (body.get("rows") or [])[: insight.MAX_ROWS]]
    return "\n".join(lines)


async def _stream_frames(request: Request, question: str, session_id: str | None, dataset_id: str, dataset: dict):
    """§4.11 的帧序：plan → sql → row_count → token* → insight → trace → done；断连即停并记 canceled。"""
    started = time.perf_counter()
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    task_id = f"t_{uuid4().hex[:8]}"
    recorded = False

    def mark_canceled(sql: str = "") -> None:
        """断开落一条 degraded=stream:canceled 记录：不写就查不出「后续模型调用到底停没停」。"""
        nonlocal recorded
        if recorded:
            return
        recorded = True
        store.insert_task(
            {
                "id": task_id,
                "dataset_id": dataset_id,
                "session_id": session_id,
                "kind": "ask",
                "question": question,
                "sql": sql,
                "degraded": ["stream:canceled"],
                "error": "客户端断开，已停止后续模型调用",
            }
        )

    try:
        yield stream.sse("plan", {"question": question, "dataset_id": dataset_id, "agent": config.ENABLE_AGENT})
        if await _gone(request):
            mark_canceled()
            return
        body = await run_in_threadpool(_ask_body, question, session_id, dataset_id, dataset)
        task_id = body.get("task_id") or task_id
        if body.get("sql"):
            yield stream.sse("sql", {"sql": body["sql"]})
        # 行数帧带上整张结果表：前端只靠这一条流就要画出表格与图表，不再多打一次 /query
        yield stream.sse(
            "row_count",
            {
                "row_count": int(body.get("row_count") or 0),
                "truncated": bool(body.get("truncated")),
                "columns": body.get("columns") or [],
                "rows": body.get("rows") or [],
            },
        )
        sent = {str(item) for item in body.get("degraded") or []}
        for tag in sorted(sent):
            yield stream.sse("degraded", {"stage": "query", "reason": tag})
        if await _gone(request):
            mark_canceled(body.get("sql") or "")
            return
        text = ""
        try:
            async for piece in llm.stream_text(STREAM_SYSTEM, _stream_prompt(question, body, profile)):
                text += piece
                yield stream.sse("token", {"text": piece})
        except (llm.LLMUnavailable, llm.LLMError) as exc:
            # 说话这一跳动不了不影响结果表：插一条 degraded，接着出结构化结论
            sent.add("stream:llm")
            body["degraded"] = [*(body.get("degraded") or []), "stream:llm"]
            yield stream.sse("degraded", {"stage": "stream", "reason": "stream:llm", "message": str(exc)})
        if await _gone(request):
            mark_canceled(body.get("sql") or "")
            return
        body = await run_in_threadpool(
            attach_insight, body, question, dataset, memory.insight_context(question, session_id)
        )
        for tag in [str(item) for item in body.get("degraded") or [] if str(item) not in sent]:
            yield stream.sse("degraded", {"stage": "insight", "reason": tag})
        yield stream.sse(
            "insight",
            {"insight": body.get("insight"), "text": text, "message": body.get("message") or ""},
        )
        # K-005：与 /ask 同口径落一行提问历史，degraded 也记进去
        await run_in_threadpool(
            store.insert_task,
            {
                "id": task_id,
                "dataset_id": dataset_id,
                "session_id": session_id,
                "kind": "ask",
                "question": question,
                "sql": body.get("sql") or "",
                "degraded": body.get("degraded") or [],
                "error": (body.get("message") or "") if body.get("degraded") else "",
            },
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        yield stream.sse("trace", {"task_id": task_id, "steps": body.get("steps") or [], "elapsed_ms": elapsed})
        yield stream.sse(
            "done",
            {
                "task_id": task_id,
                "degraded": body.get("degraded") or [],
                "message": body.get("message") or "",
                "elapsed_ms": elapsed,
            },
        )
    except (asyncio.CancelledError, GeneratorExit):
        # Starlette 收到 http.disconnect 就取消这个生成器；§4.11 要求不留后台僵尸请求
        mark_canceled()
        raise
