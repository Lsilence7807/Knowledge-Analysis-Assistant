# 文件：backend/app/api/routes/sessions.py
# 作用：会话路由：开/删会话、会话内提问（带问答复用）、看步骤轨迹与缓存命中率（§4.3 的 P10 行）
# 阶段：F4 Agent 与记忆换 LangGraph（兼 P10）
# 依赖：fastapi、app/api/deps.py、app/api/routes/{ask,insight}.py、app/schemas、app/services/*
from __future__ import annotations

from contextlib import closing
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app.api.deps import get_dataset
from app.api.routes.ask import _ask_body
from app.api.routes.insight import attach_insight
from app.core import config, db
from app.schemas import SessionAskIn, SessionIn
from app.services import cache, memory, store

router = APIRouter()


@router.post("/sessions")
def create_session(payload: SessionIn) -> dict:
    """开一个会话；dataset_id 必须存在，否则 404（会话总是绑在一个数据集上）。"""
    get_dataset(payload.dataset_id)
    return {"session": memory.create(payload.dataset_id, payload.title, payload.model_id)}


@router.post("/sessions/{sid}/ask")
def session_ask(sid: str, payload: SessionAskIn) -> dict:
    """会话内提问：同一问题先查精确缓存，命中就复用 SQL、结论重算（§4.7 的问答复用契约）。"""
    session = memory.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail=f"没有这个会话：{sid}")
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    dataset_id = payload.dataset_id or session["dataset_id"]
    dataset = get_dataset(dataset_id)
    key = cache.key(dataset_id, dataset["table_version"], session.get("model_id") or "", question)
    hit = cache.lookup(key) if config.ENABLE_CACHE else None
    reused = bool(hit and hit.get("sql"))
    if reused:
        body = _reuse_body(hit, dataset_id)
    else:
        body = _ask_body(question, sid, dataset_id, dataset)
    body = attach_insight(body, question, dataset, memory.insight_context(question, sid))
    body["session_id"] = sid
    body["cached"] = bool(reused)
    body["reused_sql"] = bool(reused)
    store.insert_task(
        {
            "id": body.get("task_id"),
            "dataset_id": dataset_id,
            "session_id": sid,
            "kind": "ask",
            "question": question,
            "sql": body.get("sql") or "",
            "degraded": body.get("degraded") or [],
            "error": (body.get("message") or "") if body.get("degraded") else "",
        }
    )
    if not reused and body.get("sql") and not body.get("degraded"):
        cache.store_hit(key, dataset_id, question, body["sql"], body.get("insight"))
    memory.add_turn(sid, question, body.get("sql") or "", body.get("columns") or [], body.get("insight"), reused)
    return body


@router.delete("/sessions/{sid}")
def delete_session(sid: str) -> dict:
    """删会话与它的轮次（步骤流水按 tasks 追溯，不跟着删）。"""
    if memory.get(sid) is None:
        raise HTTPException(status_code=404, detail=f"没有这个会话：{sid}")
    return {"session_id": sid, "deleted_turns": memory.drop(sid)}


@router.get("/sessions/{sid}/steps")
def session_steps(sid: str) -> dict:
    """会话里每一轮的 agent 步骤轨迹（P13 就把步骤落库了，这里只是按会话查出来）。"""
    if memory.get(sid) is None:
        raise HTTPException(status_code=404, detail=f"没有这个会话：{sid}")
    with closing(store.connect()) as conn:
        rows = conn.execute(
            "SELECT steps.* FROM agent_steps AS steps JOIN tasks ON tasks.id = steps.task_id"
            " WHERE tasks.session_id = ? ORDER BY steps.id",
            (sid,),
        ).fetchall()
    return {"session_id": sid, "steps": [dict(row) for row in rows]}


@router.get("/cache/stats")
def cache_stats() -> dict:
    """缓存命中率：精确层给本表统计，语义层由 F5 填（现在恒 0）。"""
    return cache.stats()


def _reuse_body(hit: dict, dataset_id: str) -> dict:
    """命中缓存的那条：SQL 与结论复用，结果表按当前数据重跑（表版本变了不会命中）。"""
    try:
        result = db.exec_sql(str(hit["sql"]))
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "task_id": f"t_{uuid4().hex[:8]}",
        "dataset_id": dataset_id,
        **result,
        "steps": [],
        "cached": True,
        "reused_sql": True,
        "degraded": [],
        "message": "",
    }
