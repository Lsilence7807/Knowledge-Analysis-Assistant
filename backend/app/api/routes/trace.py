# 文件：backend/app/api/routes/trace.py
# 作用：调用台账端点：GET /trace/stats（花了多少钱、慢在哪）与 GET /trace/{task_id}（某次任务的调用明细）
# 阶段：F8 观测与评测（兼 P16）
# 依赖：fastapi、app/services/trace.py
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services import trace

router = APIRouter()


# 注意顺序：/trace/stats 必须排在 /trace/{task_id} 前面，否则会被当成 task_id 吸走
@router.get("/trace/stats")
def trace_stats(days: int = 7) -> dict:
    """最近 days 天的成本与耗时汇总（设计 §4.3 的 P16 端点）。"""
    if days < 1:
        raise HTTPException(status_code=400, detail="days 至少为 1")
    return trace.stats(days)


@router.get("/trace/{task_id}")
def trace_detail(task_id: str) -> dict:
    """某个任务的调用明细；查不到给空列表——台账是诊断信息，不区分「没这个任务」与「没记到 span」。"""
    return {"task_id": task_id, "spans": trace.spans(task_id)}
