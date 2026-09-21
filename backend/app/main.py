# 文件：backend/app/main.py
# 作用：应用工厂：装配 api 路由、生命周期与前端静态目录；只做装配，端点实现全在 api/routes/
# 阶段：F1 后端骨架（原 app/main.py 的 554 行路由拆进 api/routes/*.py）
#       F7 装配限流（slowapi）、跨域（CORS，按需）与「开了认证没设口令就拒绝启动」的自检
#       F9 起装配后台作业：启动钩子、huey 消费者与 APScheduler
# 依赖：FastAPI、slowapi、app/api/__init__.py、app/core/{config,security}.py、app/services/store.py
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import api_router
from app.api.routes.ask import _stream_frames  # noqa: F401  test_p17_stream 按改造前的名字引用它
from app.core import config, db, security
from app.services import cache, jobs, memory, store


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时准备数据目录、跑迁移、兜建表；迁移失败即启动失败，不带病运行。"""
    # 公开端开了认证却没设口令：宁可起不来，也不要以「看起来要登录」的姿态裸奔（§7、§10）
    if security.auth_enabled() and not security.password_configured():
        raise RuntimeError("已开认证（ENABLE_AUTH=true）但没设 APP_PASSWORD_HASH：先设口令再启动")
    config.ensure_dirs()
    db.migrate()
    store.ensure_tables()
    memory.ensure_tables()
    cache.ensure_tables()
    if jobs.enabled():
        # P18：先把上次没跑完的作业标 interrupted，再起队列消费者与定时维护
        store.mark_interrupted()
        jobs.start_worker()
        jobs.start_scheduler()
    yield
    if jobs.enabled():
        jobs.stop_scheduler()
        jobs.stop_worker()


BUILD_HINT = (
    "前端还没构建：在仓库根跑 `npm --prefix frontend ci && npm --prefix frontend run build`，或直接双击 启动.cmd。"
)


# 页面 HTML 一律不缓存：/datasets、/skills 的页面路径与接口同 URL，浏览器会把文档缓存拿去应答页面的 fetch
NO_STORE = {"cache-control": "no-store"}

# 与接口同名的页面路径：GET /datasets、GET /skills 都是真接口，浏览器导航要先给页面（见 spa_over_api）
SPA_PATHS = frozenset({"/datasets", "/skills"})


def create_app() -> FastAPI:
    """装配应用：API 路由先挂，前端产物垫底，最后兜 SPA 回退。"""
    # 开认证时关掉框架自带的 schema 与 Swagger：这三条路由挂在 app 上，
    # 不吃 api_router 的认证依赖（§4.3 只放行四项，其余一律要 cookie）
    schema_on = not security.auth_enabled()
    application = FastAPI(
        title="Knowledge Analysis Assistant",
        version="0.1.0",
        openapi_url="/openapi.json" if schema_on else None,
        docs_url="/docs" if schema_on else None,
        redoc_url="/redoc" if schema_on else None,
        lifespan=lifespan,
    )
    # slowapi 的规矩：限流器与 429 处理器挂到 app 上；中间件只在公开端装（本地自用没必要给自己添 429）
    application.state.limiter = security.limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    if security.auth_enabled():
        application.add_middleware(SlowAPIMiddleware)
    # 跨域只在配了白名单时开：默认同源部署（前端由本服务一起给）不需要 CORS，也不给通配后门
    origins = [item.strip() for item in config.CORS_ORIGINS.split(",") if item.strip()]
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    application.include_router(api_router)

    @application.middleware("http")
    async def spa_over_api(request: Request, call_next):
        """页面路径与接口路径撞名时（见 SPA_PATHS），浏览器导航先给页面，别甩一坨 JSON 回去。

        页面路径是 F2 定的、接口路径是 §4.3 冻结的，所以按 Accept 分流：浏览器导航带 text/html，
        fetch 与脚本客户端不带，接口契约一个字不变。
        """
        wants_html = "text/html" in (request.headers.get("accept") or "")
        index = config.FRONTEND_DIST / "index.html"
        if request.method == "GET" and request.url.path in SPA_PATHS and wants_html and index.is_file():
            return FileResponse(index, headers=NO_STORE)
        return await call_next(request)

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
        return FileResponse(index, headers=NO_STORE)

    return application


app = create_app()
