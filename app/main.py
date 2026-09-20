# 文件：app/main.py
# 作用：HTTP 路由与编排，唯一装配点；禁止在此出现 pandas 调用与 SQL 字符串
# 阶段：P0 骨架与契约冻结（P1 加数据集路由，P2 加查询路由，P3 加提问路由，P13 加 /tools 与 agent 路径）
# 依赖：FastAPI、app/{agent,config,db,ingest,llm,nlu,store,tools}.py
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

from pathlib import Path
from uuid import uuid4

from fastapi import Body, FastAPI, File, HTTPException, UploadFile

from app import agent, config, db, ingest, llm, nlu, registry, store, tools
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
    """提问 →（模型）→ SQL → 结果表；模型不可用或 SQL 不合法时降级，HTTP 仍 200。"""
    dataset_id = str(payload.get("dataset_id") or "")
    question = str(payload.get("question") or "").strip()
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    if config.ENABLE_AGENT:
        # P13：/ask 走 agent 循环；关掉 ENABLE_AGENT 就落到下面 P3 的单跳通道（调试与降级用）
        return asdict(agent.run(question, str(payload.get("session_id") or "") or None, dataset_id))
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
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    try:
        sql = nlu.to_sql(question, profile, [])
    except (llm.LLMUnavailable, llm.LLMError, nlu.NLUError) as exc:
        # 模型这条路整体降级：手写 SQL 的 /query 不受影响，用户仍能拿到结果
        return {**base, "degraded": ["query:llm"], "message": str(exc)}
    try:
        result = db.exec_sql(sql)
    except db.SQLRejected as exc:
        return {
            **base,
            "degraded": ["query:llm"],
            "message": f"模型给出的 SQL 未通过校验，已拒绝执行：{exc}",
        }
    return {
        **base,
        "sql": result["sql"],
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "truncated": result["truncated"],
    }
