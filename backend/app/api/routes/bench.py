# 文件：backend/app/api/routes/bench.py
# 作用：性能基线端点（读 bench/baseline.json，没跑过压测时给结构化说明而不是报错）
# 阶段：F1 后端骨架（原 app/main.py 的 /bench 原样搬来）
# 依赖：fastapi、app/api/deps.py、标准库 json
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from app.api.deps import get_settings

router = APIRouter()


@router.get("/bench")
def bench_baseline(settings=Depends(get_settings)) -> dict:
    """返回最近一次性能基线（bench/baseline.json）；没跑过压测时给结构化说明，不报错。"""
    path = settings.BENCH_BASELINE
    if not path.exists():
        return {"available": False, "hint": "还没跑过压测：python bench/bench.py"}
    try:
        return {"available": True, "baseline": json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "hint": f"基线读不出来：{exc}"}
