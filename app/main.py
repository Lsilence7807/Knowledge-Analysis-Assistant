# 文件：app/main.py
# 作用：HTTP 路由与编排，唯一装配点；禁止在此出现 pandas 调用与 SQL 字符串
# 阶段：P0 骨架与契约冻结（P1 加数据集路由，P2 加查询路由，P3 加提问路由，P13 加 /tools 与 agent 路径，P4 加 /insight 并把结论并入 /ask，P5 加静态前端与 /settings/models）
# 依赖：FastAPI、app/{agent,config,db,ingest,insight,llm,models,nlu,store,tools}.py
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

from pathlib import Path
from uuid import uuid4

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

from app import agent, config, db, ingest, insight, llm, models, nlu, registry, store, tools
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


@app.post("/datasets")
async def upload_dataset(file: UploadFile = File(...)) -> dict:
    """接收文件并返回清洗后的数据集画像。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ingest.ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="只支持 CSV、XLSX 或 XLS 文件")
    content = await file.read()
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过上传大小限制")
    config.ensure_dirs()
    target = config.DATA_DIR / "files" / f"upload_{uuid4().hex}{suffix}"
    target.write_bytes(content)
    try:
        return ingest.ingest_file(target, file.filename or target.name)
    except ingest.IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/datasets")
def datasets() -> list[dict]:
    """返回已上传数据集列表。"""
    return store.list_datasets()


@app.get("/datasets/{dataset_id}/profile")
def dataset_profile(dataset_id: str) -> dict:
    """返回指定数据集的画像与清洗日志。"""
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return {
        "dataset_id": dataset["id"],
        "table": dataset["table_name"],
        "rows": dataset["rows"],
        "cols": dataset["cols"],
        "profile": dataset["profile"],
        "clean_log": dataset["clean_log"],
        "table_version": dataset["table_version"],
    }


@app.post("/query")
def run_query(payload: dict = Body(...)) -> dict:
    """直接执行一条只读 SQL；被守卫拦下或执行失败返回 400，错误信息可读。"""
    try:
        return db.exec_sql(str(payload.get("sql") or ""))
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/stats")
def dataset_stats(payload: dict = Body(...)) -> dict:
    """返回数据集的描述统计与异常行；数据集不存在返回 404。"""
    dataset_id = str(payload.get("dataset_id") or "")
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    try:
        result = db.describe(dataset_id, payload.get("columns"))
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"dataset_id": dataset_id, "rows": dataset["rows"], **result}


@app.get("/tools")
def list_tools() -> dict:
    """已注册工具、白名单与预算；供前端步骤面板与排障查看，只读不执行。"""
    cfg = tools.settings()
    return {
        "tools": tools.all_tools(set(cfg.get("allow") or [])),
        "kinds": tools.kinds(),
        "allow": [str(name) for name in cfg.get("allow") or []],
        "max_steps": int(cfg.get("max_steps", 6)),
        "max_rows_per_step": int(cfg.get("max_rows_per_step", 200)),
    }


@app.post("/ask")
def ask(payload: dict = Body(...)) -> dict:
    """提问 →（模型）→ SQL → 结果表 → 结论；模型不可用或 SQL 不合法时降级，HTTP 仍 200。"""
    dataset_id = str(payload.get("dataset_id") or "")
    question = str(payload.get("question") or "").strip()
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    if config.ENABLE_AGENT:
        # P13：/ask 走 agent 循环；关掉 ENABLE_AGENT 就落到下面 P3 的单跳通道（调试与降级用）
        body = asdict(agent.run(question, str(payload.get("session_id") or "") or None, dataset_id))
    else:
        base = {
            "task_id": f"t_{uuid4().hex[:8]}",
            "dataset_id": dataset_id,
            "sql": "",
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "steps": [],
            "cached": False,
            "reused_sql": False,
            "degraded": [],
            "message": "",
        }
        try:
            sql = nlu.to_sql(question, profile, [])
        except (llm.LLMUnavailable, llm.LLMError, nlu.NLUError) as exc:
            # 模型这条路整体降级：手写 SQL 的 /query 不受影响，用户仍能拿到结果
            body = {**base, "degraded": ["query:llm"], "message": str(exc)}
        else:
            try:
                result = db.exec_sql(sql)
            except db.SQLRejected as exc:
                body = {
                    **base,
                    "degraded": ["query:llm"],
                    "message": f"模型给出的 SQL 未通过校验，已拒绝执行：{exc}",
                }
            else:
                body = {
                    **base,
                    "sql": result["sql"],
                    "columns": result["columns"],
                    "rows": result["rows"],
                    "row_count": result["row_count"],
                    "truncated": result["truncated"],
                }
    return _attach_insight(body, question, dataset)


@app.post("/insight")
def explain(payload: dict = Body(...)) -> dict:
    """对一条 SQL 的结果表出结论；模型不可用时 insight 为空并记 degraded，HTTP 仍 200。"""
    dataset_id = str(payload.get("dataset_id") or "")
    question = str(payload.get("question") or "").strip()
    sql = str(payload.get("sql") or "").strip()
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if not sql:
        raise HTTPException(status_code=400, detail="需要 sql：/insight 解读的是查询结果")
    try:
        result = db.exec_sql(sql)
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _attach_insight({"dataset_id": dataset_id, **result, "degraded": [], "message": ""}, question, dataset)


def _attach_insight(body: dict, question: str, dataset: dict) -> dict:
    """给结果表补 insight：没有行就不解读；模型失败只追加 degraded，绝不改表格结果。"""
    body.setdefault("insight", None)
    if not body.get("rows"):
        return body
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    try:
        body["insight"] = insight.summarize(question, body, profile, []).model_dump()
    except insight.InsightError as exc:
        body["degraded"] = [*body.get("degraded", []), exc.code]
        body["message"] = body.get("message") or str(exc)
    return body


@app.get("/settings/models")
def list_model_settings() -> dict:
    """可用模型 profile：密钥只回「是否已配」，不回明文。"""
    return {"models": [_public_model(model) for model in models.list_models()]}


@app.put("/settings/models")
def save_model_settings(payload: dict = Body(...)) -> dict:
    """新增或更新一个 OpenAI 兼容 profile、写密钥到 config/local.json，并设为默认；保存即生效。"""
    profile_id = str(payload.get("id") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    model_name = str(payload.get("model") or "").strip()
    if not (profile_id and base_url and model_name):
        raise HTTPException(status_code=400, detail="id、base_url、model 都是必填")
    profile = {
        "id": profile_id,
        "provider": str(payload.get("provider") or "openai_compatible"),
        "base_url": base_url,
        "model": model_name,
        "purpose": payload.get("purpose") or ["sql", "insight"],
    }
    if payload.get("api_key_env"):
        profile["api_key_env"] = str(payload["api_key_env"])
    patch = {"models": [profile], "default": profile_id}
    if "api_key" in payload:
        # 传了 api_key 才写盘（空串等于清掉，好让页面能演示「没密钥只出表格」）
        patch["api_keys"] = {profile_id: str(payload.get("api_key") or "")}
    config.save_settings(**patch)
    return list_model_settings()


def _public_model(model: dict) -> dict:
    """对外只给 id/厂商/模型名与是否已配密钥，密钥本体不出网关。"""
    return {
        "id": model.get("id"),
        "provider": model.get("provider"),
        "base_url": model.get("base_url"),
        "model": model.get("model"),
        "has_key": bool(config.api_key(model)),
    }


# 静态前端必须挂在最后：Mount("/") 会兜住所有未匹配路径，排在 API 路由之前会把它们全抢走
if (config.BASE_DIR / "web").is_dir():
    app.mount("/", StaticFiles(directory=config.BASE_DIR / "web", html=True), name="web")
