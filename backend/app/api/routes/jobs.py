# 文件：backend/app/api/routes/jobs.py
# 作用：后台作业端点：POST /jobs 提交（长分析转后台）、GET /jobs/{job_id} 查进度与结果
# 阶段：F9 作业与文档（兼 P18）
# 依赖：fastapi、app/api/deps.py、app/services/store.py
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import require_capability
from app.services import store

router = APIRouter()


@router.post("/jobs")
def submit_job(payload: dict = Body(default={}), module=Depends(require_capability("jobs"))) -> dict:
    """提交后台作业（现在只支持 `ask`：一次完整分析）。

    预判耗时超阈值且队列消费者在跑就进队列，否则当场跑完——两条路都回同样的进度结构。
    """
    job_payload = dict(payload.get("payload") or {})
    dataset_id = str(job_payload.get("dataset_id") or "")
    if dataset_id:
        dataset = store.get_dataset(dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="数据集不存在")
        # 预判耗时要用行数：客户端不必知道数据集多大，这里补上
        job_payload.setdefault("rows", dataset.get("rows") or 0)
    try:
        job_id = module.submit(str(payload.get("kind") or "ask"), job_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job_id": job_id, **module.progress(job_id)}


@router.get("/jobs/{job_id}")
def job_progress(job_id: str, module=Depends(require_capability("jobs"))) -> dict:
    """作业进度与结果；不存在给 404（P18 反例口径）。"""
    try:
        return module.progress(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"作业不存在：{job_id}") from exc
