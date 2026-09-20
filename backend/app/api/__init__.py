# 文件：backend/app/api/__init__.py
# 作用：API 路由汇总：每个路由文件一条 include_router；新端点进对应文件，新文件加在末尾（热点文件，只追加）
# 阶段：F1 后端骨架
# 依赖：fastapi、app/api/routes/*.py
from fastapi import APIRouter

from app.api.routes import (
    ask,
    bench,
    capabilities,
    datasets,
    health,
    insight,
    kb,
    mcp,
    metrics,
    models,
    query,
    sandbox,
    skills,
    tools,
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(capabilities.router)
api_router.include_router(datasets.router)
api_router.include_router(query.router)
api_router.include_router(ask.router)
api_router.include_router(insight.router)
api_router.include_router(models.router)
api_router.include_router(skills.router)
api_router.include_router(kb.router)
api_router.include_router(mcp.router)
api_router.include_router(sandbox.router)
api_router.include_router(metrics.router)
api_router.include_router(tools.router)
api_router.include_router(bench.router)
