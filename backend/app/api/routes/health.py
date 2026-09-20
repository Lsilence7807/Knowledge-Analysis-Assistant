# 文件：backend/app/api/routes/health.py
# 作用：存活检查端点
# 阶段：F1 后端骨架（原 app/main.py 的 /health 原样搬来）
# 依赖：fastapi、app/schemas/__init__.py
from fastapi import APIRouter

from app.schemas import HealthOut

router = APIRouter()


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """存活检查。"""
    return HealthOut()
