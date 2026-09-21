# 文件：deploy/Dockerfile
# 作用：多阶段镜像：node 阶段构建前端 → python 阶段装锁定依赖、起 uvicorn（单进程同时服务 API 与页面）
# 阶段：F10 部署骨架
# 依赖：frontend/（Vite 产物落 web/dist）、requirements.lock、backend/、config/
# 构建：docker build -f deploy/Dockerfile -t datacopilot .
FROM node:22-alpine AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# vite.config.ts 的 outDir 是 ../web/dist，产物落在 /src/web/dist
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app
COPY requirements.lock requirements-heavy.lock ./
# 依赖分档（§10 FRAMEWORK_PROFILE，2026-09-22 拆档）：core 只装应用 + 轻解析后端，full 额外装 Docling 那一串（GB 级）
ARG FRAMEWORK_PROFILE=core
ENV FRAMEWORK_PROFILE=${FRAMEWORK_PROFILE}
RUN pip install --no-cache-dir -r requirements.lock \
    && if [ "$FRAMEWORK_PROFILE" = "full" ]; then pip install --no-cache-dir -r requirements-heavy.lock; fi
COPY backend/ backend/
COPY config/ config/
COPY evals/ evals/
COPY examples/ examples/
COPY web/ web/
COPY --from=frontend /src/web/dist /app/web/dist
# 数据与配置都挂卷：重建容器不丢数据集、作业库与页面写入的密钥
VOLUME ["/app/data", "/app/config"]
EXPOSE 8000
# 健康检查走应用自己的 /health（它是免认证路径）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health').read()"
# 单进程：uvicorn 同时服务 API 与前端静态文件（web/dist），不另起 nginx
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
