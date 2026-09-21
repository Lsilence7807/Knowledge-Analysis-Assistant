# 文件：tests/test_f1_skeleton.py
# 作用：F1 验收测试：搬进 backend/app 并按 api/routes 拆分后，/openapi.json 的路由表与改造前逐条一致
# 阶段：F1 后端骨架
# 依赖：pytest、fastapi.testclient（依赖 httpx）、backend/app/main.py
from __future__ import annotations

from app.main import app

# 改造前 app/main.py 里那 23 个端点的路径与方法；§4.3 的 HTTP 形状是冻结契约，拆文件不许改它
# F4 加了 5 条会话与缓存路径（23 -> 28 个端点 / 25 条路径），以后新段加端点照样往这张表加一行
# F7 加了登录与登出（25 -> 27 条路径，P11）；F8 加了 /trace/* 与 /evals/*（27 -> 31 条，P16）
FROZEN_ROUTES: dict[str, set[str]] = {
    "/ask": {"post"},
    "/ask/stream": {"get"},
    "/bench": {"get"},
    "/cache/stats": {"get"},
    "/capabilities": {"get"},
    "/datasets": {"get", "post"},
    "/datasets/{dataset_id}": {"delete"},
    "/datasets/{dataset_id}/profile": {"get"},
    "/evals/report": {"get"},
    "/evals/run": {"post"},
    "/health": {"get"},
    "/insight": {"post"},
    "/kb/import": {"post"},
    "/kb/search": {"post"},
    "/login": {"post"},
    "/logout": {"post"},
    "/mcp/call": {"post"},
    "/mcp/tools": {"get"},
    "/metrics/definitions": {"get"},
    "/query": {"post"},
    "/sandbox/run": {"post"},
    "/sessions": {"post"},
    "/sessions/{sid}": {"delete"},
    "/sessions/{sid}/ask": {"post"},
    "/sessions/{sid}/steps": {"get"},
    "/settings/models": {"get", "put"},
    "/skills": {"get", "post"},
    "/stats": {"post"},
    "/tools": {"get"},
    "/trace/stats": {"get"},
    "/trace/{task_id}": {"get"},
}


def test_openapi_routes_unchanged():
    """路径与方法集合必须一条不多一条不少：F2 的 TS 客户端靠这份 openapi 生成，改形状等于改前端契约。"""
    paths = app.openapi()["paths"]
    assert {path: set(spec) for path, spec in paths.items()} == FROZEN_ROUTES
