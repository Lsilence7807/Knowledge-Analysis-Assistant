# 文件：app/main.py
# 作用：HTTP 路由与编排，唯一装配点；禁止在此出现 pandas 调用与 SQL 字符串
# 阶段：P0 骨架与契约冻结（P1 加数据集路由，P2 加查询路由，P3 加提问路由，P13 加 /tools 与 agent 路径，
#       P4 加 /insight 并把结论并入 /ask，P5 加静态前端与 /settings/models；A 类补丁加 /ask 落 tasks 与
#       DELETE /datasets/{id}，K-013 四个路由换 pydantic 请求体，P6 加 /skills，P7 加 /kb/*，P17 加 /ask/stream）
# 依赖：FastAPI、app/{agent,config,db,ingest,insight,llm,models,nlu,store,stream,tools}.py
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import agent, config, db, ingest, insight, llm, models, nlu, registry, store, stream, tools
from app.schemas import AskIn, CapabilitiesOut, HealthOut, InsightIn, QueryIn, StatsIn


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
        # 清洗失败就没有数据集指向这个文件，留着只会变孤儿（K-015）
        target.unlink(missing_ok=True)
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


@app.delete("/datasets/{dataset_id}")
def drop_dataset(dataset_id: str) -> dict:
    """删除数据集：DuckDB 表、原始上传文件与元数据一起清（K-015 最小版）；不存在返回 404。"""
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    source = Path(str(dataset.get("source_path") or ""))
    removed_source = source.is_file()
    if removed_source:
        source.unlink(missing_ok=True)
    db.drop_table(dataset_id)
    store.delete_dataset(dataset_id)
    return {
        "dataset_id": dataset_id,
        "table": dataset["table_name"],
        "deleted": True,
        "removed_source": removed_source,
    }


@app.post("/query")
def run_query(payload: QueryIn) -> dict:
    """直接执行一条只读 SQL；被守卫拦下或执行失败返回 400，错误信息可读。"""
    try:
        return db.exec_sql(payload.sql)
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/stats")
def dataset_stats(payload: StatsIn) -> dict:
    """返回数据集的描述统计与异常行；数据集不存在返回 404。"""
    dataset = store.get_dataset(payload.dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    try:
        result = db.describe(payload.dataset_id, payload.columns)
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"dataset_id": payload.dataset_id, "rows": dataset["rows"], **result}


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


@app.get("/bench")
def bench_baseline() -> dict:
    """返回最近一次性能基线（bench/baseline.json）；没跑过压测时给结构化说明，不报错。"""
    path = config.BENCH_BASELINE
    if not path.exists():
        return {"available": False, "hint": "还没跑过压测：python bench/bench.py"}
    try:
        return {"available": True, "baseline": json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "hint": f"基线读不出来：{exc}"}


@app.get("/skills")
def skills_list() -> dict:
    """已导入技能清单；ENABLE_SKILLS 关闭时 503 并给开启方式，不猜也不返回空列表。"""
    return {"skills": _skills_capability().list_skills()}


@app.post("/skills")
def skills_import(payload: dict = Body(default={})) -> dict:
    """导入技能目录（默认 skills/）：解析每个子目录的 SKILL.md 入库，坏的单独跳过。"""
    module = _skills_capability()
    try:
        return module.import_dir(str(payload.get("path") or ""))
    except module.SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _skills_capability():
    """取技能能力；开关关闭时 registry 返回 None，这里换成可读的 503（与 /capabilities 口径一致）。"""
    module = registry.get("skills")
    if module is None:
        raise HTTPException(status_code=503, detail="技能能力未启用：设 ENABLE_SKILLS=true 再重启服务")
    return module


@app.post("/kb/import")
def kb_import(payload: dict = Body(default={})) -> dict:
    """导入一份文档或一个目录下的 md/txt；单个文件坏了解只跳过它，返回 imported 与 skipped。"""
    module = _kb_capability()
    try:
        return module.import_path(str(payload.get("path") or ""))
    except module.KbError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/kb/search")
def kb_search(payload: dict = Body(default={})) -> dict:
    """关键词检索知识库，回带出处的片段（已按 §7 包不可信标记）；检索词为空返回 400。"""
    module = _kb_capability()
    try:
        return module.search(str(payload.get("query") or ""), payload.get("limit"))
    except module.KbError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _kb_capability():
    """取知识库能力；开关关闭时 registry 返回 None，换成可读的 503（与 /capabilities 口径一致）。"""
    module = registry.get("kb")
    if module is None:
        raise HTTPException(status_code=503, detail="知识库能力未启用：设 ENABLE_KB=true 再重启服务")
    return module


@app.post("/ask")
def ask(payload: AskIn) -> dict:
    """提问 →（模型）→ SQL → 结果表 → 结论；模型不可用或 SQL 不合法时降级，HTTP 仍 200。"""
    dataset_id = payload.dataset_id
    question = payload.question.strip()
    session_id = payload.session_id
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    body = _ask_body(question, session_id, dataset_id, dataset)
    body = _attach_insight(body, question, dataset)
    # K-005：Agent 路径原本不写 tasks，提问历史只留在响应里；这里落一行，degraded 也记进去
    degraded = body.get("degraded") or []
    store.insert_task(
        {
            "id": body.get("task_id"),
            "dataset_id": dataset_id,
            "session_id": session_id,
            "kind": "ask",
            "question": question,
            "sql": body.get("sql") or "",
            "degraded": degraded,
            # message 在成功时是模型的收尾说明，只有降级时它才是错误原因，别混进 error 列
            "error": (body.get("message") or "") if degraded else "",
        }
    )
    return body


def _ask_body(question: str, session_id: str | None, dataset_id: str, dataset: dict) -> dict:
    """提问 →（agent 或单跳）→ 结果表；/ask 与 /ask/stream 共用这一段，两条路口径必须一致。"""
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    if config.ENABLE_AGENT:
        # P13：/ask 走 agent 循环；关掉 ENABLE_AGENT 就落到下面 P3 的单跳通道（调试与降级用）
        return asdict(agent.run(question, session_id or None, dataset_id))
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
        return {**base, "degraded": ["query:llm"], "message": str(exc)}
    try:
        result = db.exec_sql(sql)
    except db.SQLRejected as exc:
        return {**base, "degraded": ["query:llm"], "message": f"模型给出的 SQL 未通过校验，已拒绝执行：{exc}"}
    return {**base, **{key: result[key] for key in ("sql", "columns", "rows", "row_count", "truncated")}}


@app.post("/insight")
def explain(payload: InsightIn) -> dict:
    """对一条 SQL 的结果表出结论；模型不可用时 insight 为空并记 degraded，HTTP 仍 200。"""
    dataset_id = payload.dataset_id
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    try:
        result = db.exec_sql(payload.sql)
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _attach_insight(
        {"dataset_id": dataset_id, **result, "degraded": [], "message": ""},
        payload.question.strip(),
        dataset,
    )


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


# ---- P17 流式提问（帧序见 §4.11）----
STREAM_SYSTEM = """你在解读一张数据分析的结果表，说给用户听。
规则：只用给出的结果表说话；先一句话结论，再给两三条依据，最后说注意事项；不要编造表里没有的数字。"""


@app.get("/ask/stream")
async def ask_stream(
    request: Request,
    question: str = "",
    dataset_id: str = "",
    session_id: str = "",
) -> StreamingResponse:
    """流式提问（SSE）；模型不可用或 SQL 不合法只在流里插 degraded 帧，HTTP 仍是 200。"""
    if not stream.ENABLED:
        raise HTTPException(status_code=503, detail="流式能力未启用：设 ENABLE_STREAM=true 再重启服务")
    # 校验发生在开流之前：数据集不存在、问题为空仍然是普通 HTTP 错误，不用从流里猜
    dataset = await run_in_threadpool(store.get_dataset, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    question = question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    return StreamingResponse(
        _stream_frames(request, question, session_id or None, dataset_id, dataset),
        media_type="text/event-stream",
        headers=stream.SSE_HEADERS,
    )


async def _gone(request: Request) -> bool:
    """客户端是否已断开：Starlette 的标记只能由 is_disconnected 刷新，所以先 await 一次再同步读。"""
    await request.is_disconnected()
    return stream.canceled(request)


def _stream_prompt(question: str, body: dict, profile: dict) -> str:
    """流式结论的提示：问题 + SQL + 结果表前几行；行数上限与 insight 共用，别让两条路看到不同的表。"""
    lines = [
        f"数据集：{profile.get('table', '')}",
        f"问题：{question}",
        f"SQL：{body.get('sql') or '（没有可用的 SQL）'}",
        f"结果表：{len(body.get('rows') or [])} 行，列：{'、'.join(str(name) for name in body.get('columns') or [])}",
    ]
    lines += [json.dumps(row, ensure_ascii=False, default=str) for row in (body.get("rows") or [])[: insight.MAX_ROWS]]
    return "\n".join(lines)


async def _stream_frames(request: Request, question: str, session_id: str | None, dataset_id: str, dataset: dict):
    """§4.11 的帧序：plan → sql → row_count → token* → insight → trace → done；断连即停并记 canceled。"""
    started = time.perf_counter()
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    task_id = f"t_{uuid4().hex[:8]}"
    recorded = False

    def mark_canceled(sql: str = "") -> None:
        """断开落一条 degraded=stream:canceled 记录：不写就查不出「后续模型调用到底停没停」。"""
        nonlocal recorded
        if recorded:
            return
        recorded = True
        store.insert_task(
            {
                "id": task_id,
                "dataset_id": dataset_id,
                "session_id": session_id,
                "kind": "ask",
                "question": question,
                "sql": sql,
                "degraded": ["stream:canceled"],
                "error": "客户端断开，已停止后续模型调用",
            }
        )

    try:
        yield stream.sse("plan", {"question": question, "dataset_id": dataset_id, "agent": config.ENABLE_AGENT})
        if await _gone(request):
            mark_canceled()
            return
        body = await run_in_threadpool(_ask_body, question, session_id, dataset_id, dataset)
        task_id = body.get("task_id") or task_id
        if body.get("sql"):
            yield stream.sse("sql", {"sql": body["sql"]})
        # 行数帧带上整张结果表：前端只靠这一条流就要画出表格与图表，不再多打一次 /query
        yield stream.sse(
            "row_count",
            {
                "row_count": int(body.get("row_count") or 0),
                "truncated": bool(body.get("truncated")),
                "columns": body.get("columns") or [],
                "rows": body.get("rows") or [],
            },
        )
        sent = {str(item) for item in body.get("degraded") or []}
        for tag in sorted(sent):
            yield stream.sse("degraded", {"stage": "query", "reason": tag})
        if await _gone(request):
            mark_canceled(body.get("sql") or "")
            return
        text = ""
        try:
            async for piece in llm.stream_text(STREAM_SYSTEM, _stream_prompt(question, body, profile)):
                text += piece
                yield stream.sse("token", {"text": piece})
        except (llm.LLMUnavailable, llm.LLMError) as exc:
            # 说话这一跳动不了不影响结果表：插一条 degraded，接着出结构化结论
            sent.add("stream:llm")
            body["degraded"] = [*(body.get("degraded") or []), "stream:llm"]
            yield stream.sse("degraded", {"stage": "stream", "reason": "stream:llm", "message": str(exc)})
        if await _gone(request):
            mark_canceled(body.get("sql") or "")
            return
        body = await run_in_threadpool(_attach_insight, body, question, dataset)
        for tag in [str(item) for item in body.get("degraded") or [] if str(item) not in sent]:
            yield stream.sse("degraded", {"stage": "insight", "reason": tag})
        yield stream.sse(
            "insight",
            {"insight": body.get("insight"), "text": text, "message": body.get("message") or ""},
        )
        # K-005：与 /ask 同口径落一行提问历史，degraded 也记进去
        await run_in_threadpool(
            store.insert_task,
            {
                "id": task_id,
                "dataset_id": dataset_id,
                "session_id": session_id,
                "kind": "ask",
                "question": question,
                "sql": body.get("sql") or "",
                "degraded": body.get("degraded") or [],
                "error": (body.get("message") or "") if body.get("degraded") else "",
            },
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        yield stream.sse("trace", {"task_id": task_id, "steps": body.get("steps") or [], "elapsed_ms": elapsed})
        yield stream.sse(
            "done",
            {
                "task_id": task_id,
                "degraded": body.get("degraded") or [],
                "message": body.get("message") or "",
                "elapsed_ms": elapsed,
            },
        )
    except (asyncio.CancelledError, GeneratorExit):
        # Starlette 收到 http.disconnect 就取消这个生成器；§4.11 要求不留后台僵尸请求
        mark_canceled()
        raise


# 静态前端必须挂在最后：Mount("/") 会兜住所有未匹配路径，排在 API 路由之前会把它们全抢走
if (config.BASE_DIR / "web").is_dir():
    app.mount("/", StaticFiles(directory=config.BASE_DIR / "web", html=True), name="web")
