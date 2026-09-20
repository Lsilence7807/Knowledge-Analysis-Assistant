# 文件：backend/app/api/routes/tools.py
# 作用：已注册工具与白名单端点（供前端步骤面板与排障查看，只读不执行）
# 阶段：F1 后端骨架（原 app/main.py 的 /tools 原样搬来）
# 依赖：fastapi、app/services/tools.py
from fastapi import APIRouter

from app.services import tools

router = APIRouter()


@router.get("/tools")
def list_tools() -> dict:
    """已注册工具、白名单与预算；供前端步骤面板与排障查看，只读不执行。"""
    cfg = tools.settings()
    return {
        "tools": tools.all_tools(set(cfg.get("allow") or [])),
        "kinds": tools.kinds(),
        "allow": [str(name) for name in cfg.get("allow") or []],
        "max_steps": int(cfg.get("max_steps", 6)),
        "max_rows_per_step": int(cfg.get("max_rows_per_step", 200)),
    }
