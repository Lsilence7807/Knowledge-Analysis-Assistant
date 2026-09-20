# 文件：backend/app/services/cache.py
# 作用：问答复用缓存：精确键命中直接复用 SQL；语义召回留接口给 F5（LanceDB/LiteLLM cache）
# 阶段：F4 Agent 与记忆换 LangGraph（兼 P10；F5 加语义层）
# 依赖：hashlib、json、logging、re、sqlalchemy、app/core/{config,db,vectors}.py、app/models
from __future__ import annotations

import hashlib
import json
import logging
import re

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core import config, db, vectors
from app.models import Dataset, QaCache
from app.services import llm

logger = logging.getLogger(__name__)

PUNCT = re.compile(r"[\s，。？！、：；,.?!:;\"'（）()\[\]【】]+")  # 只影响归一化，不删字


def ensure_tables() -> None:
    """建缓存表，幂等（表统一由 store 那一处建）。"""
    from app.services import store

    store.ensure_tables()


def norm(question: str) -> str:
    """归一化问题：去标点空白、统一大小写，同义改写不管（那是语义层的事）。"""
    return PUNCT.sub("", (question or "").strip().lower())


def key(dataset_id: str, table_version: int, model_id: str, question: str) -> str:
    """精确缓存键：数据集 + 表版本 + 模型 + 问题（表版本变了自然不命中）。"""
    raw = f"{dataset_id}|{int(table_version)}|{model_id}|{norm(question)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lookup(k: str) -> dict | None:
    """按键取一条命中并累加计数；没有返回 None。"""
    with db.session() as orm:
        row = orm.get(QaCache, k)
        if row is None:
            return None
        orm.execute(
            update(QaCache).where(QaCache.key == k).values(hits=QaCache.hits + 1, last_hit_at=func.current_timestamp())
        )
        orm.commit()
        result = {column.name: getattr(row, column.name) for column in QaCache.__table__.columns}
    return result


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
    with db.session() as orm:
        dataset = orm.get(Dataset, dataset_id)
        values = {
            "key": k,
            "dataset_id": dataset_id,
            "table_version": int(dataset.table_version or 1) if dataset else 1,
            "norm_question": norm(question),
            "model_id": "",
            "sql": sql,
            "insight_json": json.dumps(insight or {}, ensure_ascii=False),
        }
        statement = sqlite_insert(QaCache).values(**values)
        orm.execute(
            statement.on_conflict_do_update(
                index_elements=[QaCache.key],
                set_={
                    "sql": statement.excluded.sql,
                    "insight_json": statement.excluded.insight_json,
                    "table_version": statement.excluded.table_version,
                },
            )
        )
        orm.commit()


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
    with db.session() as orm:
        dataset = orm.get(Dataset, dataset_id)
    return int(dataset.table_version or 1) if dataset else 1


def invalidate(dataset_id: str) -> int:
    """删掉某数据集的全部缓存，返回条数（删数据集/重导入时用）。"""
    with db.session() as orm:
        removed = orm.execute(delete(QaCache).where(QaCache.dataset_id == dataset_id))
        orm.commit()
    return max(0, int(removed.rowcount or 0))


def _vector_rows() -> int:
    """语义层索引里有多少条（表还没建过就是 0）。"""
    try:
        return vectors.row_count(vectors.store("qa_cache"))
    except (OSError, ImportError, ValueError, vectors.TableNotFoundError):
        return 0


def stats() -> dict:
    """命中口径：精确层给条数、总命中与命中率；语义层 F5 才填。"""
    with db.session() as orm:
        totals = select(func.count(), func.coalesce(func.sum(QaCache.hits), 0)).select_from(QaCache)
        total, hits = orm.execute(totals).one()
    entries = int(total or 0)
    hits = int(hits or 0)
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
