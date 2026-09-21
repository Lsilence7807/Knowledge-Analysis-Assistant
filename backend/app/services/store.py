# 文件：backend/app/services/store.py
# 作用：元数据层：SQLAlchemy ORM 读写（表由 Alembic 基线建）；kb_fts 是 FTS5 虚表，只能用 text() 原始 SQL
# 阶段：P0 骨架与契约冻结（扩展阶段在此追加新表；A 类补丁加 tasks 写入与数据集删除）
#       P6 加 skills 表；P7 加 kb_docs 与检索索引 kb_fts；F6 换 SQLAlchemy ORM + Alembic（表结构一行没改）
# 依赖：sqlalchemy、backend/app/core/{config,db}.py、backend/app/models/__init__.py
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core import config, db
from app.models import AgentStep, CapabilityLog, Dataset, EvalRun, Job, KbDoc, Skill, Task, TraceSpan, Turn

logger = logging.getLogger(__name__)

# FTS5 虚表 ORM 表达不了（MATCH、rowid 绑定、bm25），这一条留原始 DDL，其余表由 ORM 的 metadata 建
FTS_DDL = "CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(content, tokenize='trigram')"


def connect() -> sqlite3.Connection:
    """裸连接：只给 FTS5 维护与测试用例用（ORM 走 core/db.session()）。"""
    config.ensure_dirs()
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables() -> None:
    """建表，幂等：ORM 的 metadata 建普通表，再补 FTS5 虚表。"""
    from app.models import Base

    Base.metadata.create_all(db.engine())
    with db.session() as session:
        session.execute(text(FTS_DDL))
        session.commit()


def table_names() -> list[str]:
    """当前库里的表名，供自检与测试使用。"""
    with db.session() as session:
        rows = session.execute(text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")).fetchall()
    return [row[0] for row in rows]


def insert_dataset(dataset: dict) -> None:
    """写入一个数据集的元数据。"""
    ensure_tables()
    fields = (
        "id",
        "name",
        "source_id",
        "source_path",
        "table_name",
        "rows",
        "cols",
        "profile_json",
        "clean_log",
        "table_version",
        "owner",
    )
    with db.session() as session:
        session.add(Dataset(**{field: dataset.get(field) for field in fields}))
        session.commit()


def list_datasets() -> list[dict]:
    """返回所有数据集的简要元数据。"""
    ensure_tables()
    columns = (
        Dataset.id,
        Dataset.name,
        Dataset.table_name,
        Dataset.rows,
        Dataset.cols,
        Dataset.table_version,
        Dataset.created_at,
    )
    with db.session() as session:
        rows = session.execute(select(*columns).order_by(Dataset.created_at.desc(), Dataset.id.desc())).all()
    return [dict(row._mapping) for row in rows]


def get_dataset(dataset_id: str) -> dict | None:
    """返回指定数据集及其画像，不存在时返回 None。"""
    ensure_tables()
    with db.session() as session:
        row = session.execute(select(Dataset).where(Dataset.id == dataset_id)).scalar_one_or_none()
    if row is None:
        return None
    result = {column.name: getattr(row, column.name) for column in Dataset.__table__.columns}
    result["profile"] = json.loads(result.pop("profile_json") or "{}")
    result["clean_log"] = json.loads(result["clean_log"] or "[]")
    return result


def insert_task(task: dict) -> None:
    """写一条提问任务（K-005）；status 由 degraded 是否有内容决定，写失败只记日志，不拖垮提问。"""
    try:
        ensure_tables()
        degraded = [str(item) for item in task.get("degraded") or []]
        with db.session() as session:
            session.merge(
                Task(
                    id=task.get("id"),
                    dataset_id=task.get("dataset_id"),
                    session_id=task.get("session_id") or "",
                    kind=task.get("kind") or "ask",
                    question=task.get("question") or "",
                    sql=task.get("sql") or "",
                    status="degraded" if degraded else "ok",
                    degraded_json=json.dumps(degraded, ensure_ascii=False),
                    error=task.get("error") or "",
                )
            )
            session.commit()
    except SQLAlchemyError as exc:
        logger.warning("任务写入失败(%s)：%s", task.get("id"), exc)


def delete_dataset(dataset_id: str) -> bool:
    """删掉数据集元数据及其任务与步骤流水（K-015），返回是否删到了行。"""
    ensure_tables()
    with db.session() as session:
        task_ids = select(Task.id).where(Task.dataset_id == dataset_id)
        session.execute(delete(AgentStep).where(AgentStep.task_id.in_(task_ids)))
        session.execute(delete(Task).where(Task.dataset_id == dataset_id))
        removed = session.execute(delete(Dataset).where(Dataset.id == dataset_id))
        session.commit()
    return bool(removed.rowcount)


def insert_agent_step(task_id: str, step: dict) -> None:
    """写一条 agent 步骤流水；args_json 存的是参数摘要（设计的 args_digest），完整参数落库没意义。"""
    ensure_tables()
    with db.session() as session:
        session.add(
            AgentStep(
                task_id=task_id,
                n=step.get("n"),
                tool=step.get("tool"),
                args_json=step.get("args_digest"),
                ok=int(bool(step.get("ok"))),
                ms=step.get("ms"),
                rows=step.get("rows"),
                error=step.get("error", ""),
            )
        )
        session.commit()


def log_capability(capability: str, event: str, detail: str = "") -> None:
    """写一条能力流水（越权调用、能力上下线等）；这是诊断信息，写失败不能拖垮主流程。"""
    try:
        ensure_tables()
        with db.session() as session:
            session.add(CapabilityLog(capability=capability, event=event, detail=detail))
            session.commit()
    except SQLAlchemyError as exc:
        logger.warning("能力流水写入失败(%s/%s)：%s", capability, event, exc)


def upsert_skill(skill: dict) -> None:
    """按 slug 写入或更新一条技能；同一目录重复导入只更新，不产生重复行。"""
    ensure_tables()
    slug = str(skill.get("slug"))
    with db.session() as session:
        session.merge(
            Skill(
                id=skill.get("id") or f"sk_{slug}",
                slug=slug,
                path=skill.get("path"),
                description=skill.get("description") or "",
                kind=skill.get("kind") or "prompt",
                enabled=int(bool(skill.get("enabled", 1))),
            )
        )
        session.commit()


def list_skills() -> list[dict]:
    """返回已导入的技能，按 slug 排序。"""
    ensure_tables()
    columns = (Skill.id, Skill.slug, Skill.path, Skill.description, Skill.kind, Skill.enabled, Skill.created_at)
    with db.session() as session:
        rows = session.execute(select(*columns).order_by(Skill.slug)).all()
    return [dict(row._mapping) for row in rows]


def get_skill(slug: str) -> dict | None:
    """按 slug 取一条技能，没有时返回 None。"""
    ensure_tables()
    with db.session() as session:
        row = session.execute(select(Skill).where(Skill.slug == slug)).scalar_one_or_none()
    return {column.name: getattr(row, column.name) for column in Skill.__table__.columns} if row else None


def replace_kb_doc(doc: dict, chunks: list[str]) -> int:
    """替换一份文档的全部分块（重复导入只更新、不产生重复块），返回写入的块数。"""
    ensure_tables()
    with db.session() as session:
        old_ids = session.execute(select(KbDoc.id).where(KbDoc.path == doc.get("path"))).scalars().all()
        for row_id in old_ids:
            session.execute(text("DELETE FROM kb_fts WHERE rowid = :rowid"), {"rowid": row_id})
        session.execute(delete(KbDoc).where(KbDoc.path == doc.get("path")))
        for index, text_chunk in enumerate(chunks, start=1):
            row = KbDoc(
                path=doc.get("path"),
                title=doc.get("title"),
                chunk_no=index,
                content=text_chunk,
                source_type=doc.get("source_type"),
            )
            session.add(row)
            session.flush()  # 先拿到自增 id，FTS 行要按同一个 rowid 对齐
            # 正文在两处各存一份：kb_docs 给人看与取正文，kb_fts 只管检索
            session.execute(
                text("INSERT INTO kb_fts (rowid, content) VALUES (:rowid, :content)"),
                {"rowid": row.id, "content": text_chunk},
            )
        session.commit()
    return len(chunks)


def search_kb(query: str, limit: int = 5) -> list[dict]:
    """FTS5 检索（bm25 排序）；查询词短于 3 个字符时退回 LIKE（trigram 建不出那么短的词）。"""
    ensure_tables()
    if len(query) >= 3:
        # 整串当短语查：输入里的 FTS 语法字符（*、"、AND/OR）不再是语法，少了注入面
        statement = text(
            "SELECT d.path, d.title, d.chunk_no, d.content, bm25(kb_fts) AS score "
            "FROM kb_fts JOIN kb_docs d ON d.id = kb_fts.rowid "
            "WHERE kb_fts MATCH :phrase ORDER BY bm25(kb_fts) LIMIT :limit"
        )
        params = {"phrase": '"' + query.replace('"', '""') + '"', "limit": limit}
    else:
        statement = text(
            "SELECT d.path, d.title, d.chunk_no, d.content, 0.0 AS score "
            "FROM kb_docs d WHERE d.content LIKE :like ORDER BY d.id LIMIT :limit"
        )
        params = {"like": f"%{query}%", "limit": limit}
    with db.session() as session:
        rows = session.execute(statement, params).fetchall()
    return [dict(row._mapping) for row in rows]


def count_kb_chunks() -> int:
    """已入库的知识库块数，供自检与测试使用。"""
    ensure_tables()
    with db.session() as session:
        return int(session.execute(select(func.count()).select_from(KbDoc)).scalar_one())


def insert_trace_span(
    task_id: str,
    name: str,
    *,
    model_id: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
    ms: int = 0,
    ok: bool = True,
) -> None:
    """记一条调用台账（F8）：一次模型或工具调用一行，成本与耗时都留在这里。"""
    ensure_tables()
    with db.session() as session:
        session.add(
            TraceSpan(
                task_id=task_id,
                name=name,
                model_id=model_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost=cost,
                ms=ms,
                ok=int(bool(ok)),
            )
        )
        session.commit()


def _span_row(row: TraceSpan) -> dict:
    """ORM 行转字典：created_at 保持 SQLite 的字符串，前端与统计都按字符串切日期。"""
    return {
        "id": row.id,
        "task_id": row.task_id,
        "name": row.name,
        "model_id": row.model_id,
        "tokens_in": row.tokens_in or 0,
        "tokens_out": row.tokens_out or 0,
        "cost": row.cost or 0.0,
        "ms": row.ms or 0,
        "ok": bool(row.ok),
        "created_at": row.created_at or "",
    }


def trace_spans(task_id: str) -> list[dict]:
    """某个任务的调用明细（GET /trace/{task_id}），按发生顺序回。"""
    with db.session() as session:
        rows = session.execute(select(TraceSpan).where(TraceSpan.task_id == task_id).order_by(TraceSpan.id)).scalars()
        return [_span_row(row) for row in rows]


def trace_stats(days: int = 7) -> dict:
    """最近 days 天的台账汇总：花了多少钱、慢在哪（GET /trace/stats 的数据源）。

    created_at 是 SQLite 的 UTC 文本，时间下界也按 UTC 算，免得本机时区把当天的数据切掉。
    """
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db.session() as session:
        rows = session.execute(select(TraceSpan).where(TraceSpan.created_at >= since)).scalars()
        spans = [_span_row(row) for row in rows]

    by_name: dict[str, dict] = {}
    by_day: dict[str, float] = {}
    for span in spans:
        key = span["name"] or "-"
        item = by_name.setdefault(key, {"name": key, "spans": 0, "cost": 0.0, "ms": 0})
        item["spans"] += 1
        item["cost"] += span["cost"]
        item["ms"] += span["ms"]
        day = (span["created_at"] or "")[:10]
        by_day[day] = by_day.get(day, 0.0) + span["cost"]
    for item in by_name.values():
        item["cost"] = round(item["cost"], 6)
        item["ms_avg"] = round(item["ms"] / item["spans"]) if item["spans"] else 0

    slowest = sorted(spans, key=lambda item: item["ms"], reverse=True)[:5]
    return {
        "days": days,
        "spans": len(spans),
        "cost_total": round(sum(span["cost"] for span in spans), 6),
        "tokens_in": sum(span["tokens_in"] for span in spans),
        "tokens_out": sum(span["tokens_out"] for span in spans),
        "by_name": sorted(by_name.values(), key=lambda item: item["cost"], reverse=True),
        "by_day": [{"day": day, "cost": round(cost, 6)} for day, cost in sorted(by_day.items())],
        "slowest": [
            {"name": span["name"], "ms": span["ms"], "task_id": span["task_id"], "created_at": span["created_at"]}
            for span in slowest
        ],
    }


def insert_job(job: dict) -> None:
    """写入一个作业（jobs 表，§4.4）：入参与结果都序列化成 JSON 存一行。"""
    ensure_tables()
    with db.session() as session:
        session.add(
            Job(
                id=job["id"],
                kind=job.get("kind"),
                payload_json=json.dumps(job.get("payload") or {}, ensure_ascii=False),
                status=job.get("status") or "queued",
                progress=float(job.get("progress") or 0.0),
                result_json=json.dumps(job.get("result") or {}, ensure_ascii=False),
                error=job.get("error") or "",
            )
        )
        session.commit()


def _job_row(row: Job) -> dict:
    """ORM 行转字典：payload 与 result 都还原成对象，调用方不用再解一遍 JSON。"""
    return {
        "id": row.id,
        "kind": row.kind,
        "payload": json.loads(row.payload_json or "{}"),
        "status": row.status,
        "progress": row.progress or 0.0,
        "result": json.loads(row.result_json) if row.result_json else {},
        "error": row.error or "",
        "created_at": row.created_at or "",
        "updated_at": row.updated_at or "",
    }


def get_job(job_id: str) -> dict | None:
    """按 id 取作业，没有回 None（路由翻成 404）。"""
    with db.session() as session:
        row = session.get(Job, job_id)
        return _job_row(row) if row is not None else None


def update_job(job_id: str, **fields) -> dict | None:
    """改状态/进度/结果并刷新 updated_at；作业不存在回 None。"""
    with db.session() as session:
        row = session.get(Job, job_id)
        if row is None:
            return None
        if "status" in fields:
            row.status = fields["status"]
        if "progress" in fields:
            row.progress = float(fields["progress"])
        if "error" in fields:
            row.error = fields["error"]
        if "result" in fields:
            row.result_json = json.dumps(fields["result"] or {}, ensure_ascii=False)
        row.updated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        session.commit()
        return _job_row(row)


def list_jobs(limit: int = 20) -> list[dict]:
    """最近的作业，新的在前。"""
    with db.session() as session:
        rows = session.execute(select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit)).scalars()
        return [_job_row(row) for row in rows]


def find_active_job(kind: str, payload: dict) -> dict | None:
    """同一入参已在排队/执行中的作业：重复提交时回同一个 job_id（P18 反例口径）。

    payload_json 的键序不保证稳定，比对前按 sort_keys 归一，免得顺序不同就当成两次提交。
    """
    wanted = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    with db.session() as session:
        rows = session.execute(select(Job).where(Job.kind == kind, Job.status.in_(("queued", "running")))).scalars()
        for row in rows:
            if json.dumps(json.loads(row.payload_json or "{}"), ensure_ascii=False, sort_keys=True) == wanted:
                return _job_row(row)
    return None


def mark_interrupted() -> int:
    """启动钩子：上次没跑完的作业标 interrupted（重启后它们再也不会被执行）。"""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with db.session() as session:
        rows = session.execute(select(Job).where(Job.status.in_(("queued", "running")))).scalars().all()
        for row in rows:
            row.status = "interrupted"
            row.error = row.error or "服务重启时作业还没跑完"
            row.updated_at = stamp
        session.commit()
        return len(rows)


def prune_jobs(days: int = 7) -> int:
    """清掉 N 天前的终态作业（APScheduler 每天调一次）；排队与执行中的一律不动。"""
    since = (datetime.now(UTC) - timedelta(days=max(int(days), 0))).strftime("%Y-%m-%d %H:%M:%S")
    with db.session() as session:
        result = session.execute(
            delete(Job).where(Job.status.in_(("done", "failed", "interrupted")), Job.created_at < since)
        )
        session.commit()
        return int(result.rowcount or 0)


def get_task(task_id: str) -> dict | None:
    """按 id 取一行任务（导出报告要问题与 SQL）。"""
    with db.session() as session:
        row = session.get(Task, task_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "dataset_id": row.dataset_id,
            "session_id": row.session_id,
            "kind": row.kind,
            "question": row.question,
            "sql": row.sql,
            "status": row.status,
            "degraded": json.loads(row.degraded_json or "[]"),
            "error": row.error,
            "created_at": row.created_at,
        }


def latest_insight(session_id: str, question: str) -> str:
    """某次提问的结论摘要（导出报告用）：turns 里按会话与问题取最近一条，没有回空串。"""
    if not session_id or not question:
        return ""
    with db.session() as session:
        row = (
            session.execute(
                select(Turn)
                .where(Turn.session_id == session_id, Turn.question == question)
                .order_by(Turn.seq.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
    return (row.insight_summary or "") if row is not None else ""


def insert_eval_run(run: dict) -> None:
    """记一轮评测（F8）：指标与基线差值都落 eval_runs，前端与 /evals/report 从这里读。"""
    ensure_tables()
    with db.session() as session:
        session.add(
            EvalRun(
                golden_path=run.get("golden_path"),
                total=run.get("total"),
                sql_pass_rate=run.get("sql_pass_rate"),
                fidelity_rate=run.get("fidelity_rate"),
                degrade_rate=run.get("degrade_rate"),
                cost_total=run.get("cost_total"),
                baseline_delta_json=json.dumps(run.get("baseline_delta") or {}, ensure_ascii=False),
            )
        )
        session.commit()


def list_eval_runs(limit: int = 10) -> list[dict]:
    """最近的评测记录，新的在前。"""
    with db.session() as session:
        rows = session.execute(select(EvalRun).order_by(EvalRun.id.desc()).limit(limit)).scalars()
        return [
            {
                "id": row.id,
                "golden_path": row.golden_path,
                "total": row.total,
                "sql_pass_rate": row.sql_pass_rate,
                "fidelity_rate": row.fidelity_rate,
                "degrade_rate": row.degrade_rate,
                "cost_total": row.cost_total,
                "baseline_delta": json.loads(row.baseline_delta_json or "{}"),
                "created_at": row.created_at,
            }
            for row in rows
        ]
