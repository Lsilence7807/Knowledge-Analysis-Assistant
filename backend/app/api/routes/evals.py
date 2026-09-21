# 文件：backend/app/api/routes/evals.py
# 作用：评测端点：POST /evals/run 跑一轮 golden，GET /evals/report 看历史与当前基线
# 阶段：F8 观测与评测（兼 P16）
# 依赖：fastapi、app/core/config.py、app/services/{evalharness,store}.py
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from app.core import config
from app.services import evalharness, store

router = APIRouter()


@router.post("/evals/run")
def run_evals() -> dict:
    """跑一轮 golden 集（§4.10）。

    低于基线也回 200：基线是质量门禁（命令行 exit 1），不是 HTTP 错误——调用方看 breached 列表。
    """
    try:
        return evalharness.run_golden(str(config.EVAL_GOLDEN), str(config.EVAL_BASELINE))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"评测跑不起来：{exc}") from exc


@router.get("/evals/report")
def evals_report(limit: int = 10) -> dict:
    """最近的评测记录 + 当前基线（前端评测页读它）；基线文件坏了不影响看历史。"""
    try:
        baseline = json.loads(config.EVAL_BASELINE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        baseline = {}
    return {"baseline": baseline, "runs": store.list_eval_runs(limit)}
