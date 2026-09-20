# 文件：backend/app/api/routes/sandbox.py
# 作用：受限代码执行端点（未启用 503，代码被拒 400）
# 阶段：F1 后端骨架（原 app/main.py 的 /sandbox/run 原样搬来）
# 依赖：fastapi、app/api/deps.py、app/schemas/__init__.py
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_capability
from app.schemas import SandboxIn

router = APIRouter()


@router.post("/sandbox/run")
def sandbox_run(payload: SandboxIn, module=Depends(require_capability("sandbox"))) -> dict:
    """受限执行一段 pandas 代码：dataset_id 非空就把它只读注入为 df；未启用 503，代码被拒 400。"""
    try:
        return module.run_code(payload.code, dataset_id=payload.dataset_id)
    except module.SandboxError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
