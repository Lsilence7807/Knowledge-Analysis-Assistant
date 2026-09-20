# 文件：backend/app/models/tables.py
# 作用：§4.4 数据模型的 ORM 映射（当前代码用到的表；其余表随各自阶段加，别提前建空表）
# 阶段：F6 数据层换 SQLAlchemy + Alembic（表结构与 §4.4 逐列一致，一行没改）
# 依赖：sqlalchemy、backend/app/models/base.py
from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

CREATED_AT = "CURRENT_TIMESTAMP"


class Dataset(Base):
    """数据集元数据（datasets）。"""

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String)
    source_path: Mapped[str | None] = mapped_column(String)
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    rows: Mapped[int | None] = mapped_column(Integer)
    cols: Mapped[int | None] = mapped_column(Integer)
    profile_json: Mapped[str | None] = mapped_column(Text)
    clean_log: Mapped[str | None] = mapped_column(Text)
    table_version: Mapped[int | None] = mapped_column(Integer, default=1)
    owner: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class Task(Base):
    """提问任务流水（tasks）。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    dataset_id: Mapped[str | None] = mapped_column(String)
    session_id: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str | None] = mapped_column(String)
    question: Mapped[str | None] = mapped_column(Text)
    sql: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String)
    degraded_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class AgentStep(Base):
    """agent 步骤流水（agent_steps）。"""

    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    n: Mapped[int | None] = mapped_column(Integer)
    tool: Mapped[str | None] = mapped_column(String)
    args_json: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[int | None] = mapped_column(Integer)
    ms: Mapped[int | None] = mapped_column(Integer)
    rows: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class TraceSpan(Base):
    """成本耗时台账（trace_spans，P16 用）。"""

    __tablename__ = "trace_spans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    model_id: Mapped[str | None] = mapped_column(String)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column()
    ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class CapabilityLog(Base):
    """能力流水（capability_log）。"""

    __tablename__ = "capability_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    capability: Mapped[str | None] = mapped_column(String)
    event: Mapped[str | None] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class Skill(Base):
    """技能库（skills）。"""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True)
    path: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(String)
    enabled: Mapped[int | None] = mapped_column(Integer, default=1)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class KbDoc(Base):
    """知识库分块（kb_docs）；检索索引 kb_fts 是 FTS5 虚表，ORM 表达不了，由 DDL 建。"""

    __tablename__ = "kb_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    chunk_no: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(String)
    vector: Mapped[bytes | None] = mapped_column()
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class SessionRow(Base):
    """会话（sessions；类名避开 SQLAlchemy 的 Session）。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    dataset_id: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    model_id: Mapped[str | None] = mapped_column(String)
    owner: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)
    last_active_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class Turn(Base):
    """会话轮次（turns）。"""

    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    seq: Mapped[int | None] = mapped_column(Integer)
    question: Mapped[str | None] = mapped_column(Text)
    sql: Mapped[str | None] = mapped_column(Text)
    columns_json: Mapped[str | None] = mapped_column(Text)
    insight_summary: Mapped[str | None] = mapped_column(Text)
    cached: Mapped[int | None] = mapped_column(Integer, default=0)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)


class QaCache(Base):
    """问答复用缓存（qa_cache）。"""

    __tablename__ = "qa_cache"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    dataset_id: Mapped[str | None] = mapped_column(String)
    table_version: Mapped[int | None] = mapped_column(Integer)
    norm_question: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(String)
    sql: Mapped[str | None] = mapped_column(Text)
    insight_json: Mapped[str | None] = mapped_column(Text)
    vector: Mapped[bytes | None] = mapped_column()
    hits: Mapped[int | None] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)
    created_at: Mapped[str | None] = mapped_column(Text, server_default=CREATED_AT)
