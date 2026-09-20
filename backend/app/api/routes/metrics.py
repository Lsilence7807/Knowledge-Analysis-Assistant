# 文件：backend/app/api/routes/metrics.py
# 作用：指标口径端点（config/metrics.json 原文，只读）
# 阶段：F1 后端骨架（原 app/main.py 的 /metrics/definitions 原样搬来）
# 依赖：fastapi、app/services/metrics.py
from fastapi import APIRouter

from app.services import metrics

router = APIRouter()


@router.get("/metrics/definitions")
def metrics_definitions() -> dict:
    """指标口径清单（config/metrics.json 原文）；未定义的说法归 tools 标注，这里不编公式。"""
    return {"definitions": metrics.definitions()}
