# 文件：backend/app/services/cache.py
# 作用：问答复用缓存：精确键命中直接复用 SQL；语义召回留接口给 F5（LanceDB/LiteLLM cache）
# 阶段：F4 Agent 与记忆换 LangGraph（兼 P10；F5 加语义层）
# 依赖：hashlib、json、re、app/core/config.py、app/services/{insight,store}.py
from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing

from app.services import store

PUNCT = re.compile(r"[\s，。？！、：；,.?!:;\"'（）()\[\]【】]+")  # 只影响归一化，不删字
DDL = (
    """CREATE TABLE IF NOT EXISTS qa_cache (
        key TEXT PRIMARY KEY, dataset_id TEXT, table_version INTEGER, norm_question TEXT, model_id TEXT,
        sql TEXT, insight_json TEXT, vector BLOB, hits INTEGER DEFAULT 0,
        last_hit_at TEXT DEFAULT CURRENT_TIMESTAMP, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
)


def ensure_tables() -> None:
    """建缓存表，幂等（main 启动时调一次）。"""
    with closing(store.connect()) as conn:
        for stmt in DDL:
            conn.execute(stmt)
        conn.commit()


def norm(question: str) -> str:
    """归一化问题：去标点空白、统一大小写，同义改写不管（那是语义层的事）。"""
    return PUNCT.sub("", (question or "").strip().lower())


def key(dataset_id: str, table_version: int, model_id: str, question: str) -> str:
    """精确缓存键：数据集 + 表版本 + 模型 + 问题（表版本变了自然不命中）。"""
    raw = f"{dataset_id}|{int(table_version)}|{model_id}|{norm(question)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lookup(k: str) -> dict | None:
    """按键取一条命中并累加计数；没有返回 None。"""
    with closing(store.connect()) as conn:
        row = conn.execute("SELECT * FROM qa_cache WHERE key = ?", (k,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE qa_cache SET hits = hits + 1, last_hit_at = CURRENT_TIMESTAMP WHERE key = ?", (k,))
        conn.commit()
    return dict(row)


def lookup_semantic(dataset_id: str, table_version: int, vector: list[float], threshold: float = 0.93) -> dict | None:
    """语义命中：向量库由 F5 落地，这里先恒不命中（回退精确键）。"""
    return None


def store_hit(
    k: str, dataset_id: str, question: str, sql: str, insight: dict | None, vector: list[float] | None = None
) -> None:
    """写一条缓存；表版本取数据集当前版本（失效靠它而不是删表）。"""
    ensure_tables()
    with closing(store.connect()) as conn:
        dataset = conn.execute("SELECT table_version FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        conn.execute(
            "INSERT INTO qa_cache (key, dataset_id, table_version, norm_question, model_id, sql, insight_json, vector)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " sql = excluded.sql, insight_json = excluded.insight_json, table_version = excluded.table_version",
            (
                k,
                dataset_id,
                int(dataset["table_version"] if dataset else 1),
                norm(question),
                "",
                sql,
                json.dumps(insight or {}, ensure_ascii=False),
                None,
            ),
        )
        conn.commit()


def invalidate(dataset_id: str) -> int:
    """删掉某数据集的全部缓存，返回条数（删数据集/重导入时用）。"""
    with closing(store.connect()) as conn:
        removed = conn.execute("DELETE FROM qa_cache WHERE dataset_id = ?", (dataset_id,)).rowcount
        conn.commit()
    return max(0, int(removed))


def stats() -> dict:
    """命中口径：精确层给条数、总命中与命中率；语义层 F5 才填。"""
    with closing(store.connect()) as conn:
        row = conn.execute("SELECT count(*) AS n, coalesce(sum(hits), 0) AS hits FROM qa_cache").fetchone()
    hits = int(row["hits"] or 0)
    entries = int(row["n"] or 0)
    return {
        "exact": {
            "entries": entries,
            "hits": hits,
            "hit_rate": round(hits / (hits + entries), 4) if entries or hits else 0.0,
        },
        "semantic": {"entries": 0, "hits": 0, "hit_rate": 0.0, "enabled": False},
    }
