# 文件：backend/app/api/__init__.py
# 作用：API 路由汇总：每个路由文件一条 include_router；新端点进对应文件，新文件加在末尾（热点文件，只追加）
# 阶段：F1 后端骨架；F7 给整个 api_router 挂认证依赖（§4.3 里认证列「否」的路径在 security 里放行）；
#       F8 加 trace 与 evals 两条（P16 端点）；F9 加 jobs 与 exports 两条（P18/P19 端点）
# 依赖：fastapi、app/api/routes/*.py、app/core/security.py
from fastapi import APIRouter, Depends

from app.api.routes import (
    ask,
    auth,
    bench,
    capabilities,
    datasets,
    evals,
    exports,
    health,
    insight,
    jobs,
    kb,
    mcp,
    metrics,
    models,
    query,
    sandbox,
    sessions,
    skills,
    tools,
    trace,
)
from app.core import security

# 默认要求登录：漏挂某条新路由不会静默裸奔；免认证路径写死在 security.EXEMPT_PATHS
api_router = APIRouter(dependencies=[Depends(security.require_auth)])

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
api_router.include_router(sessions.router)
api_router.include_router(bench.router)
api_router.include_router(auth.router)
api_router.include_router(trace.router)
api_router.include_router(evals.router)
api_router.include_router(jobs.router)
api_router.include_router(exports.router)
