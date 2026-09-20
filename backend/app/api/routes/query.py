# 文件：backend/app/api/routes/query.py
# 作用：直连 SQL 与统计端点（不走模型，模型挂了这两条仍可用）
# 阶段：F1 后端骨架（原 app/main.py 的 /query 与 /stats 原样搬来）
# 依赖：fastapi、app/api/deps.py、app/schemas/__init__.py、app/services/db.py
from fastapi import APIRouter, HTTPException

from app.api.deps import get_dataset
from app.core import db
from app.schemas import QueryIn, StatsIn

router = APIRouter()


@router.post("/query")
def run_query(payload: QueryIn) -> dict:
    """直接执行一条只读 SQL；被守卫拦下或执行失败返回 400，错误信息可读。"""
    try:
        return db.exec_sql(payload.sql)
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stats")
def dataset_stats(payload: StatsIn) -> dict:
    """返回数据集的描述统计与异常行；数据集不存在返回 404。"""
    dataset = get_dataset(payload.dataset_id)
    try:
        result = db.describe(payload.dataset_id, payload.columns)
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"dataset_id": payload.dataset_id, "rows": dataset["rows"], **result}
