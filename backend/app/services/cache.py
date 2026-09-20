# 文件：backend/app/services/cache.py
# 作用：问答复用缓存：精确键命中直接复用 SQL；语义召回留接口给 F5（LanceDB/LiteLLM cache）
# 阶段：F4 Agent 与记忆换 LangGraph（兼 P10；F5 加语义层）
# 依赖：hashlib、json、re、app/core/config.py、app/services/{insight,store}.py
from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import closing

from app.core import config, vectors
from app.services import llm, store

logger = logging.getLogger(__name__)

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


def question_vector(question: str) -> list[float] | None:
    """把问题转成向量；没开语义层或没有 embedding 配置时返回 None（调用方直接走精确键）。"""
    if not config.ENABLE_SEMANTIC_CACHE or not (question or "").strip():
        return None
    try:
        return llm.embed([question])[0]
    except (llm.LLMUnavailable, llm.LLMError) as exc:
        logger.warning("语义缓存不可用（回退精确键）：%s", exc)
        return None


def lookup_semantic(dataset_id: str, table_version: int, vector: list[float], threshold: float = 0.93) -> dict | None:
    """语义命中（§4.2）：向量检索用 LlamaIndex，命中的行再从 qa_cache 取回；同数据集同表版本且相似度过线才算。"""
    if not vector or not config.ENABLE_SEMANTIC_CACHE:
        return None
    try:
        from llama_index.core.vector_stores.types import VectorStoreQuery

        result = vectors.store("qa_cache").query(VectorStoreQuery(query_embedding=list(vector), similarity_top_k=5))
    except (OSError, ImportError, ValueError, vectors.TableNotFoundError) as exc:
        logger.warning("语义缓存检索失败（回退精确键）：%s", exc)
        return None
    for node, score in zip(result.nodes, result.similarities or [], strict=False):
        meta = node.metadata or {}
        if str(meta.get("dataset_id")) != str(dataset_id) or int(meta.get("table_version") or 0) != int(table_version):
            continue
        if score is None or float(score) < float(threshold):
            continue
        row = lookup(str(meta.get("key") or node.node_id))
        if row:
            return row
    return None


def store_hit(
    k: str, dataset_id: str, question: str, sql: str, insight: dict | None, vector: list[float] | None = None
) -> None:
    """写一条缓存；表版本取数据集当前版本（失效靠它而不是删表）。"""
    ensure_tables()
    _remember_vector(k, dataset_id, question, vector)
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


def _remember_vector(k: str, dataset_id: str, question: str, vector: list[float] | None) -> None:
    """把这条缓存的问题向量写进 LanceDB（语义命中的索引）；失败只记日志，精确键不受影响。"""
    if not vector or not config.ENABLE_SEMANTIC_CACHE:
        return
    try:
        from llama_index.core.schema import TextNode

        table = vectors.store("qa_cache")
        vectors.delete_ids(table, [k])
        node = TextNode(
            id_=k,
            text=norm(question),
            metadata={"key": k, "dataset_id": str(dataset_id)},
            embedding=list(vector),
        )
        node.metadata["table_version"] = int(_version(dataset_id))
        table.add([node])
    except (OSError, ImportError, ValueError, vectors.TableNotFoundError) as exc:
        logger.warning("语义缓存向量未写入（精确键照用）：%s", exc)


def _version(dataset_id: str) -> int:
    """数据集当前表版本（语义命中要比对，旧版本不能复用）。"""
    with closing(store.connect()) as conn:
        row = conn.execute("SELECT table_version FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    return int(row["table_version"] if row else 1)


def invalidate(dataset_id: str) -> int:
    """删掉某数据集的全部缓存，返回条数（删数据集/重导入时用）。"""
    with closing(store.connect()) as conn:
        removed = conn.execute("DELETE FROM qa_cache WHERE dataset_id = ?", (dataset_id,)).rowcount
        conn.commit()
    return max(0, int(removed))


def _vector_rows() -> int:
    """语义层索引里有多少条（表还没建过就是 0）。"""
    try:
        return vectors.row_count(vectors.store("qa_cache"))
    except (OSError, ImportError, ValueError, vectors.TableNotFoundError):
        return 0


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
        "semantic": {
            "entries": _vector_rows(),
            "hits": 0,
            "hit_rate": 0.0,
            "enabled": bool(config.ENABLE_SEMANTIC_CACHE),
        },
    }
