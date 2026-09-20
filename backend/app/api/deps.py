# 文件：backend/app/api/deps.py
# 作用：HTTP 边界的依赖注入：配置、元数据库连接、数据集与能力开关；路由只声明 Depends，不自己取全局
# 阶段：F1 后端骨架（get_current_user 留 F7）
# 依赖：fastapi、app/core/config.py、app/services/{registry,store}.py
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException

from app.core import config, db
from app.services import registry, store

# 能力关闭时的 503 文案：带上开启方式，调用方直接展示，不用回来翻文档
CAPABILITY_HINTS = {
    "skills": "技能能力未启用：设 ENABLE_SKILLS=true 再重启服务",
    "kb": "知识库能力未启用：设 ENABLE_KB=true 再重启服务",
    "sandbox": "代码沙箱未启用：设 ENABLE_SANDBOX=true 再重启服务",
    "mcp": "MCP 能力不可用：需要 ENABLE_MCP=true 且 config/mcp.json 里的 server 能启动",
}


def get_settings():
    """返回配置模块本身：路由按属性读常量，测试 monkeypatch config 才能生效（F6 换 Settings 对象）。"""
    return config


def get_session() -> Iterator[Any]:
    """元数据库会话（SQLAlchemy），请求结束即关；调用方自己 commit。"""
    with db.session() as orm:
        yield orm


def get_dataset(dataset_id: str) -> dict:
    """按 id 取数据集，不存在直接 404。作依赖用时 dataset_id 来自路径参数；body 场景由路由直接调用。"""
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return dataset


def require_capability(name: str):
    """返回一个依赖：能力不可用（registry 给 None）时抛可读的 503，可用时把模块交给路由。"""

    def dependency() -> Any:
        module = registry.get(name)
        if module is None:
            raise HTTPException(status_code=503, detail=CAPABILITY_HINTS[name])
        return module

    return dependency
