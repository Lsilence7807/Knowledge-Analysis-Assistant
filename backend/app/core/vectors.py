# 文件：backend/app/core/vectors.py
# 作用：向量层公共件：LlamaIndex 的 embedding 适配（厂商调用仍走 llm.embed）+ LanceDB 表句柄与按 id 删除
# 阶段：F5 检索与缓存换 LlamaIndex + LanceDB
# 依赖：llama_index.core、llama_index.vector_stores.lancedb、backend/app/core/config.py、backend/app/services/llm.py
from __future__ import annotations

from llama_index.core.embeddings import BaseEmbedding
from llama_index.vector_stores.lancedb import LanceDBVectorStore
from llama_index.vector_stores.lancedb.base import TableNotFoundError

from app.core import config

_STORES: dict[str, LanceDBVectorStore] = {}


class LiteLLMEmbedding(BaseEmbedding):
    """把 LlamaIndex 的 embedding 接口接到 llm.embed：模型、密钥、厂商全由 LiteLLM 那一层管。"""

    def _get_query_embedding(self, query: str) -> list[float]:
        from app.services import llm

        return llm.embed([query])[0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        from app.services import llm

        return llm.embed([text])[0]

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        from app.services import llm

        return llm.embed(list(texts))


def store(table_name: str) -> LanceDBVectorStore:
    """按表名取 LanceDB 句柄（进程内复用）：句柄一旦建好别重建，重开表可能把数据覆盖掉。"""
    if table_name not in _STORES:
        config.VECTORS_DIR.mkdir(parents=True, exist_ok=True)
        _STORES[table_name] = LanceDBVectorStore(uri=str(config.VECTORS_DIR), table_name=table_name)
    return _STORES[table_name]


def reset() -> None:
    """丢掉进程内的表句柄（测试换数据目录时用；生产路径不需要）。"""
    _STORES.clear()


def raw_table(vector_store: LanceDBVectorStore):
    """取底层 LanceDB 表；还没建过（第一次写入之前）返回 None，让调用方按空处理。"""
    try:
        return vector_store.table
    except TableNotFoundError:
        return None


def row_count(vector_store: LanceDBVectorStore) -> int:
    """表里有多少行；表还不存在算 0。"""
    table = raw_table(vector_store)
    return int(table.count_rows()) if table is not None else 0


def delete_ids(vector_store: LanceDBVectorStore, ids: list[str]) -> int:
    """按 id 删行，返回删掉的条数（表还没建过就算没删到）。

    自己拼谓词而不是用 `delete_nodes()`：lancedb 0.38 + llama-index 0.6 的 delete_nodes 生成的谓词
    没给字面量加引号（`id in (kb_a_1)`），LanceDB 直接报 "No field named ..."，属于上游 bug。
    """
    table = raw_table(vector_store) if ids else None
    if table is None:
        return 0
    quoted = ", ".join("'" + str(item).replace("'", "''") + "'" for item in ids)
    table.delete(f"id IN ({quoted})")
    return len(ids)
