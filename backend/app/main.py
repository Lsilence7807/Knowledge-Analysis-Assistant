# 文件：backend/app/main.py
# 作用：应用工厂：装配 api 路由、生命周期与前端静态目录；只做装配，端点实现全在 api/routes/
# 阶段：F1 后端骨架（原 app/main.py 的 554 行路由拆进 api/routes/*.py）
# 依赖：FastAPI、app/api/__init__.py、app/core/config.py、app/services/store.py
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.api.routes.ask import _stream_frames  # noqa: F401  test_p17_stream 按改造前的名字引用它
from app.core import config
from app.services import store


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时准备数据目录与元数据表；失败即启动失败，不带病运行。"""
    config.ensure_dirs()
    store.ensure_tables()
    yield


def create_app() -> FastAPI:
    """装配应用：API 路由先挂，前端静态目录垫底。"""
    application = FastAPI(title="Knowledge Analysis Assistant", version="0.1.0", lifespan=lifespan)
    application.include_router(api_router)
    # 静态前端必须挂在最后：Mount("/") 会兜住所有未匹配路径，排在 API 路由之前会把它们全抢走
    if (config.BASE_DIR / "web").is_dir():
        application.mount("/", StaticFiles(directory=config.BASE_DIR / "web", html=True), name="web")
    return application


app = create_app()
