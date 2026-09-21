# 文件：backend/app/api/routes/insight.py
# 作用：结论端点与共用助手 attach_insight（/ask、/ask/stream 都靠它给结果表补结论）
# 阶段：F1 后端骨架（原 app/main.py 的 /insight 与 _attach_insight 原样搬来，助手去掉下划线供其它路由用）
# 依赖：fastapi、app/api/deps.py、app/schemas/__init__.py、app/services/{db,insight}.py
from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, get_dataset
from app.core import db
from app.schemas import InsightIn
from app.services import insight, memory

router = APIRouter()


@router.post("/insight")
def explain(payload: InsightIn, user: CurrentUser = None) -> dict:
    """对一条 SQL 的结果表出结论；模型不可用时 insight 为空并记 degraded，HTTP 仍 200。"""
    dataset = get_dataset(payload.dataset_id, user)
    try:
        result = db.exec_sql(payload.sql)
    except db.SQLRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return attach_insight(
        {"dataset_id": payload.dataset_id, **result, "degraded": [], "message": ""},
        payload.question.strip(),
        dataset,
        memory.insight_context(payload.question.strip(), None),
    )


def attach_insight(body: dict, question: str, dataset: dict, prior: list[str] | None = None) -> dict:
    """给结果表补 insight：没有行就不解读；模型失败只追加 degraded，绝不改表格结果。

    prior 是 §4.5 承诺的上下文（轮次 + 知识库片段 + 技能 + 指标口径），由 memory 一次给全。
    """
    body.setdefault("insight", None)
    if not body.get("rows"):
        return body
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    try:
        body["insight"] = insight.summarize(question, body, profile, list(prior or [])).model_dump()
    except insight.InsightError as exc:
        body["degraded"] = [*body.get("degraded", []), exc.code]
        body["message"] = body.get("message") or str(exc)
    return body
