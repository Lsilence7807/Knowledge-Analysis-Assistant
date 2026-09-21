# 文件：backend/app/api/routes/exports.py
# 作用：导出端点：POST /exports 把一次任务的问答与结果表导成 docx/pptx/md
# 阶段：F9 作业与文档（兼 P19）
# 依赖：fastapi、app/services/report.py
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from app.services import report

router = APIRouter()


@router.post("/exports")
def export_report(payload: dict = Body(default={})) -> dict:
    """导出报告：任务不存在 404，格式不支持或那次没 SQL 都 400。"""
    fmt = str(payload.get("fmt") or "")
    try:
        path = report.export(str(payload.get("task_id") or ""), fmt)
    except report.ReportNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except report.ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": path, "fmt": fmt.strip().lower().lstrip(".")}
