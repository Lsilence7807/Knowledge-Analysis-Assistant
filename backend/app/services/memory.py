# 文件：backend/app/services/memory.py
# 作用：会话与上下文：LangGraph SqliteSaver 存图状态，sessions/turns 表存会话与轮次；
#       上下文（最近轮次 + 结论摘要 + 知识库片段 + 技能 + 指标口径）在这里一次给全（K-012/K-034）
# 阶段：F4 Agent 与记忆换 LangGraph（兼 P10）
# 依赖：json、langgraph_checkpoint_sqlite、sqlalchemy、app/core/{config,db}.py、app/models、
#       app/services/{registry,store}.py
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.core import config, db
from app.models import SessionRow, Turn
from app.services import registry, store

TURNS = 6  # 上下文里带的最近轮次数（§4.5）
SUMMARIES = 2  # 其中再带最近几轮的结论摘要（§4.5）

_saver = None


@dataclass
class Session:
    """一个会话（§4.4 的 sessions 行）。"""

    id: str
    dataset_id: str
    title: str = ""
    model_id: str = ""
    created_at: str = ""


def ensure_tables() -> None:
    """建会话相关表，幂等（表统一由 store 那一处建）。"""
    store.ensure_tables()


def checkpointer():
    """LangGraph 的 SqliteSaver：会话图状态落 data/checkpoints.sqlite，进程内复用一份连接。"""
    global _saver
    if _saver is None:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        config.ensure_dirs()
        _saver = SqliteSaver(sqlite3.connect(config.CHECKPOINT_PATH, check_same_thread=False))
        _saver.setup()
    return _saver


def create(dataset_id: str, title: str = "", model_id: str = "") -> dict:
    """开一个会话（P10 的 POST /sessions）。"""
    ensure_tables()
    session = Session(id=f"s_{uuid4().hex[:8]}", dataset_id=dataset_id, title=title.strip(), model_id=model_id)
    with db.session() as orm:
        orm.add(
            SessionRow(
                id=session.id,
                dataset_id=session.dataset_id,
                title=session.title,
                model_id=session.model_id,
            )
        )
        orm.commit()
    return asdict(session)


def get(session_id: str) -> dict | None:
    """取会话；没有就 None（路由据此回 404）。"""
    with db.session() as orm:
        row = orm.get(SessionRow, session_id)
    if row is None:
        return None
    return {column.name: getattr(row, column.name) for column in SessionRow.__table__.columns}


def drop(session_id: str) -> int:
    """删会话连同轮次，返回删掉的轮次数（P10 的 DELETE /sessions/{sid}）。"""
    ensure_tables()
    with db.session() as orm:
        removed = orm.execute(delete(Turn).where(Turn.session_id == session_id))
        orm.execute(delete(SessionRow).where(SessionRow.id == session_id))
        orm.commit()
    return max(0, int(removed.rowcount or 0))


def add_turn(session_id: str, question: str, sql: str, columns: list, insight: dict | None, cached: bool) -> None:
    """记一轮问答：问题、SQL、列名与结论摘要（下一轮的上下文与 GET /sessions/{sid} 都用它）。"""
    if not session_id:
        return
    ensure_tables()
    with db.session() as orm:
        seq = orm.execute(select(func.count()).select_from(Turn).where(Turn.session_id == session_id)).scalar_one()
        orm.add(
            Turn(
                session_id=session_id,
                seq=int(seq) + 1,
                question=question,
                sql=sql,
                columns_json=json.dumps(columns, ensure_ascii=False),
                insight_summary=str((insight or {}).get("summary") or ""),
                cached=1 if cached else 0,
            )
        )
        row = orm.get(SessionRow, session_id)
        if row is not None:
            row.last_active_at = func.current_timestamp()  # 时间由数据库取，别在 Python 里拼
        orm.commit()


def context(session_id: str, turns: int = TURNS) -> list[str]:
    """最近几轮的「问 → SQL」，最新的排最后（§4.2 的 memory.context）。"""
    if not session_id:
        return []
    with db.session() as orm:
        rows = orm.execute(
            select(Turn.question, Turn.sql).where(Turn.session_id == session_id).order_by(Turn.seq.desc()).limit(turns)
        ).all()
    return [f"问：{row.question}｜SQL：{row.sql}" for row in reversed(rows) if row.question]


def _summaries(session_id: str, turns: int = SUMMARIES) -> list[str]:
    """最近几轮的结论摘要（同一会话里「上次说华东降幅最大」这种指代要靠它）。"""
    if not session_id:
        return []
    with db.session() as orm:
        rows = orm.execute(
            select(Turn.insight_summary)
            .where(Turn.session_id == session_id, Turn.insight_summary != "")
            .order_by(Turn.seq.desc())
            .limit(turns)
        ).all()
    return [f"上一轮结论：{row.insight_summary}" for row in reversed(rows)]


def provider_context(question: str) -> list[str]:
    """启用中的知识库/技能/指标口径：两个入口（规划节点与结论节点）共用这一份，别再各查一次。

    能力关掉时 registry.get 给 None，这里零开销（默认关闭）。
    """
    if not config.ENABLE_MEMORY:
        return []  # ENABLE_MEMORY=false：退化成单轮，什么上下文都不带
    lines: list[str] = []
    kb = registry.get("kb")
    if kb is not None and question:
        hits = [str(item.get("content") or "") for item in (kb.search(question, limit=3).get("hits") or [])]
        lines += [f"知识库片段（出处见 sources）：{text}" for text in hits if text]
    skills = registry.get("skills")
    if skills is not None:
        enabled = [f"{row.get('slug')}：{row.get('description')}" for row in skills.list_skills() if row.get("enabled")]
        if enabled:
            lines.append("可用技能：" + "；".join(enabled[:5]))
    from app.services import metrics

    for item in metrics.resolve_metrics(question or ""):
        formula = item.get("formula") or item.get("expression") or ""
        lines.append(f"指标口径：{item.get('name')} = {formula}")
    return lines


def insight_context(question: str, session_id: str | None) -> list[str]:
    """结论节点要的上下文（§4.5 的输入）：轮次 + 结论摘要 + 知识库/技能/口径（清 K-012/K-034）。"""
    return [*context(session_id or ""), *_summaries(session_id or ""), *provider_context(question)]


def agent_context(question: str, session_id: str | None) -> str:
    """规划节点要的上下文：与 insight_context 同一批东西，拼成一段补进系统提示。"""
    lines = insight_context(question, session_id)
    return ("会话与参考资料（可能不完整，仅作参考）：\n" + "\n".join(f"- {line}" for line in lines)) if lines else ""


def thread_id(session_id: str | None, task_id: str) -> str:
    """LangGraph 的 thread：有会话时按会话存，否则按本次任务存（两次提问不会串上下文）。"""
    return session_id or task_id
