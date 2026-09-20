# 文件：backend/app/services/store.py
# 作用：元数据层：SQLAlchemy ORM 读写（表由 Alembic 基线建）；kb_fts 是 FTS5 虚表，只能用 text() 原始 SQL
# 阶段：P0 骨架与契约冻结（扩展阶段在此追加新表；A 类补丁加 tasks 写入与数据集删除）
#       P6 加 skills 表；P7 加 kb_docs 与检索索引 kb_fts；F6 换 SQLAlchemy ORM + Alembic（表结构一行没改）
# 依赖：sqlalchemy、backend/app/core/{config,db}.py、backend/app/models/__init__.py
from __future__ import annotations

import json
import logging
import sqlite3

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core import config, db
from app.models import AgentStep, CapabilityLog, Dataset, KbDoc, Skill, Task

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
