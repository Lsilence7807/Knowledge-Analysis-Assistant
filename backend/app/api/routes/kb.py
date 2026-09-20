# 文件：backend/app/api/routes/kb.py
# 作用：知识库端点：导入 md/txt 与关键词检索（能力开关关闭时 503 并给开启方式）
# 阶段：F1 后端骨架（原 app/main.py 的 /kb/* 端点原样搬来）
# 依赖：fastapi、app/api/deps.py
from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.deps import require_capability

router = APIRouter()


@router.post("/kb/import")
def kb_import(payload: dict = Body(default={}), module=Depends(require_capability("kb"))) -> dict:
    """导入一份文档或一个目录下的 md/txt；单个文件坏了解只跳过它，返回 imported 与 skipped。"""
    try:
        return module.import_path(str(payload.get("path") or ""))
    except module.KbError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/kb/search")
def kb_search(payload: dict = Body(default={}), module=Depends(require_capability("kb"))) -> dict:
    """关键词检索知识库，回带出处的片段（已按 §7 包不可信标记）；检索词为空返回 400。"""
    try:
        return module.search(str(payload.get("query") or ""), payload.get("limit"))
    except module.KbError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
