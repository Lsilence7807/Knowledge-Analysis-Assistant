# 文件：app/main.py
# 作用：HTTP 路由与编排，唯一装配点；禁止在此出现 pandas 调用与 SQL 字符串
# 阶段：P0 骨架与契约冻结（P1 加数据集路由，P2 加查询路由）
# 依赖：FastAPI、app/config.py、app/registry.py、app/schemas.py、app/store.py
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import config, registry, store
from app.schemas import CapabilitiesOut, HealthOut


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时准备数据目录与元数据表；失败即启动失败，不带病运行。"""
    config.ensure_dirs()
    store.ensure_tables()
    yield


app = FastAPI(title="Knowledge Analysis Assistant", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """存活检查。"""
    return HealthOut()


@app.get("/capabilities", response_model=CapabilitiesOut)
def capabilities() -> CapabilitiesOut:
    """各能力是否可用；不可用由调用方降级，不返回 5xx。"""
    return CapabilitiesOut(
        capabilities=registry.status(),
        descriptions=dict(registry.CAPABILITIES),
    )
