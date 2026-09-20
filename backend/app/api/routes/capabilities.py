# 文件：backend/app/api/routes/capabilities.py
# 作用：能力可用性端点（前端与排障靠它判断哪些能力能点）
# 阶段：F1 后端骨架（原 app/main.py 的 /capabilities 原样搬来）
# 依赖：fastapi、app/schemas/__init__.py、app/services/registry.py
from fastapi import APIRouter

from app.schemas import CapabilitiesOut
from app.services import registry

router = APIRouter()


@router.get("/capabilities", response_model=CapabilitiesOut)
def capabilities() -> CapabilitiesOut:
    """各能力是否可用；不可用由调用方降级，不返回 5xx。"""
    return CapabilitiesOut(
        capabilities=registry.status(),
        descriptions=dict(registry.CAPABILITIES),
    )
