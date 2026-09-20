# 文件：backend/app/main.py
# 作用：应用工厂：装配 api 路由、生命周期与前端静态目录；只做装配，端点实现全在 api/routes/
# 阶段：F1 后端骨架（原 app/main.py 的 554 行路由拆进 api/routes/*.py）
# 依赖：FastAPI、app/api/__init__.py、app/core/config.py、app/services/store.py
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
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


BUILD_HINT = (
    "前端还没构建：在仓库根跑 `npm --prefix frontend ci && npm --prefix frontend run build`，或直接双击 启动.cmd。"
)


def create_app() -> FastAPI:
    """装配应用：API 路由先挂，前端产物垫底，最后兜 SPA 回退。"""
    application = FastAPI(title="Knowledge Analysis Assistant", version="0.1.0", lifespan=lifespan)
    application.include_router(api_router)

    if (config.FRONTEND_DIST / "assets").is_dir():
        application.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @application.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def frontend(full_path: str) -> FileResponse | PlainTextResponse:
        """命中构建产物就给文件，其余路径（前端路由）回 index.html；产物缺失时给构建提示。

        SPA 回退必须放最后：它兜住所有未匹配路径，排在 API 路由前会把接口全抢走。
        """
        dist = config.FRONTEND_DIST
        index = dist / "index.html"
        if not index.is_file():
            return PlainTextResponse(BUILD_HINT, status_code=503)
        if full_path:
            candidate = (dist / full_path).resolve()
            # 只许取 dist 内的文件，挡掉 ../ 之类的越界路径
            if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
        return FileResponse(index)

    return application


app = create_app()
