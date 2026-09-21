# 文件：tests/test_f6_data.py
# 作用：F6 验收：Alembic 迁移建齐表、老库升级幂等、ORM 往返读写（datasets/tasks/turns/qa_cache）
# 阶段：F6 数据层换 SQLAlchemy + Alembic + pydantic-settings
#       F8 加 eval_runs（§4.4 的评测表，迁移 0002）后同步这张清单
# 依赖：pytest、sqlite3、sqlalchemy、app.core.{config,db}、app.models、app.services.{store,memory,cache}
from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import select

from app.core import config, db
from app.models import Base, Dataset, Task
from app.services import cache, memory, store

# 迁移后应有的表：§4.4 的业务表（F8 起含 eval_runs）+ FTS5 虚表 + alembic 版本表
EXPECTED_TABLES = {
    "agent_steps",
    "capability_log",
    "datasets",
    "eval_runs",
    "jobs",
    "kb_docs",
    "kb_fts",
    "qa_cache",
    "sessions",
    "skills",
    "tasks",
    "trace_spans",
    "turns",
    "alembic_version",
}

D1 = {"id": "d1", "name": "样例", "table_name": "ds_d1", "rows": 3, "cols": 2, "table_version": 2}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """隔离数据目录与元数据库；语义层关掉——本文件测的是迁移与 ORM 本身。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "CHECKPOINT_PATH", tmp_path / "checkpoints.sqlite")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_CACHE", False)


def test_migrate_creates_all_tables():
    """alembic upgrade head 后表一条不少，且 ORM 声明的每张表都在库里。"""
    db.migrate()
    names = set(store.table_names())
    # kb_fts 有 5 张 FTS5 影子表、AUTOINCREMENT 带来 sqlite_sequence，都不算业务表
    extra = {name for name in names if not name.startswith(("kb_fts_", "sqlite_"))} - EXPECTED_TABLES
    assert EXPECTED_TABLES - names == set(), "缺表"
    assert extra == set(), f"多出来的表：{sorted(extra)}"
    assert {table.name for table in Base.metadata.sorted_tables} <= names


def test_migrate_is_idempotent_on_old_db():
    """老库（改造前就建过表、有数据）跑迁移不报错：既有的表与行原样保留，缺的表补上。"""
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.execute("CREATE TABLE datasets (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("INSERT INTO datasets (id, name) VALUES ('d_old', '老库数据集')")
    conn.commit()
    conn.close()

    db.migrate()
    db.migrate()  # 第二次是 no-op：迁移可重复执行

    conn = sqlite3.connect(config.SQLITE_PATH)
    rows = conn.execute("SELECT id, name FROM datasets").fetchall()
    conn.close()
    assert rows == [("d_old", "老库数据集")]
    assert {"datasets", "turns", "kb_fts", "alembic_version"} <= set(store.table_names())


def test_orm_roundtrip_dataset_and_task():
    """datasets/tasks 的 ORM 往返：画像 JSON 解回对象、degraded 决定 status、删数据集连带流水。"""
    store.insert_dataset({**D1, "profile_json": json.dumps({"row_count": 3}), "clean_log": "[]"})
    got = store.get_dataset("d1")
    assert got is not None
    assert (got["name"], got["rows"], got["table_version"]) == ("样例", 3, 2)
    assert got["profile"] == {"row_count": 3}
    assert [item["id"] for item in store.list_datasets()] == ["d1"]

    store.insert_task({"id": "t1", "dataset_id": "d1", "question": "多少", "sql": "SELECT 1"})
    store.insert_task({"id": "t2", "dataset_id": "d1", "question": "呢", "degraded": ["no_key"]})
    with db.session() as orm:
        tasks = orm.execute(select(Task).order_by(Task.id)).scalars().all()
    assert [task.status for task in tasks] == ["ok", "degraded"]
    assert json.loads(tasks[1].degraded_json) == ["no_key"]

    assert store.delete_dataset("d1") is True
    assert store.get_dataset("d1") is None
    with db.session() as orm:
        assert orm.execute(select(Task)).scalars().all() == []
        assert orm.execute(select(Dataset)).scalars().all() == []


def test_orm_roundtrip_turns_and_cache():
    """turns/qa_cache 的 ORM 往返：轮次按 seq 排、删会话连轮次、缓存按主键 upsert 与按数据集失效。"""
    store.insert_dataset(dict(D1))
    session = memory.create("d1", title="测试会话")
    memory.add_turn(session["id"], "华东多少", "SELECT 1", ["a"], {"summary": "结论一"}, False)
    memory.add_turn(session["id"], "华南呢", "SELECT 2", ["b"], {"summary": "结论二"}, True)

    assert memory.get(session["id"])["title"] == "测试会话"
    assert memory.context(session["id"]) == ["问：华东多少｜SQL：SELECT 1", "问：华南呢｜SQL：SELECT 2"]
    assert memory.drop(session["id"]) == 2
    assert memory.get(session["id"]) is None

    key = cache.key("d1", 2, "", "华东多少")
    cache.store_hit(key, "d1", "华东多少", "SELECT 1", {"summary": "x"})
    cache.store_hit(key, "d1", "华东多少", "SELECT 2", {"summary": "y"})  # 同键重写：upsert 不报错
    hit = cache.lookup(key)
    assert hit is not None
    assert (hit["sql"], hit["table_version"]) == ("SELECT 2", 2)
    assert cache.stats()["exact"]["entries"] == 1
    assert cache.invalidate("d1") == 1
    assert cache.lookup(key) is None
