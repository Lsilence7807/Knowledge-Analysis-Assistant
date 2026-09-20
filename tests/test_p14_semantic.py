# 文件：tests/test_p14_semantic.py
# 作用：F5（P14）验收：同义问法语义命中并复用 SQL（结论重算）、无关问题不误命中、
#       知识库语义召回、关掉或缺配置时回退精确键与 FTS5
# 阶段：F5 检索与缓存换 LlamaIndex + LanceDB
# 依赖：pytest、pandas、fastapi.testclient、app.core.{config,vectors}、app.providers.kb、
#       app.services.{cache,llm,store}、app.main
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core import config, db, vectors
from app.main import app
from app.providers import kb
from app.services import cache, llm, store

FRAME = pd.DataFrame({"region": ["华东", "华南", "华北"] * 4, "amount": [10, 11, 12] * 4})
GOOD_SQL = "SELECT region, sum(amount) AS total FROM ds_d_test GROUP BY 1 ORDER BY 2 DESC"
INSIGHT = json.dumps(
    {
        "summary": "华北最高",
        "findings": [{"title": "华北最高", "detail": "48", "metric": "total", "direction": "up"}],
        "anomalies": [],
        "suggestions": [{"action": "核查华东", "rationale": "最低"}],
        "confidence": "medium",
        "caveats": [],
    },
    ensure_ascii=False,
)
# 假 embedding：按关键词分桶，同义问法落进同一个桶（真模型由 llm.embed 那一层负责，测试不出网）
BUCKETS = {"销售": [1.0, 0.0, 0.0], "税": [0.0, 1.0, 0.0], "行数": [0.0, 0.0, 1.0]}


def _fake_embed(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for text in texts:
        vector = [0.0, 0.0, 0.05]
        for key, bucket in BUCKETS.items():
            if key in text:
                vector = bucket
                break
        out.append(vector)
    return out


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """数据目录、向量库、元数据库、checkpointer 全部指向临时目录；embedding 换成假实现。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "CHECKPOINT_PATH", tmp_path / "checkpoints.sqlite")
    monkeypatch.setattr(config, "VECTORS_DIR", tmp_path / "vectors")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_CACHE", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(kb, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(kb, "ENABLED", True)
    monkeypatch.setattr(llm, "embed", _fake_embed)
    vectors.reset()


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_dataset(dataset_id: str = "d_test") -> str:
    store.ensure_tables()
    table = db.register_table(dataset_id, FRAME)
    store.insert_dataset(
        {
            "id": dataset_id,
            "name": "t.csv",
            "table_name": table,
            "rows": len(FRAME),
            "cols": len(FRAME.columns),
            "profile_json": json.dumps({"rows": len(FRAME), "cols": len(FRAME.columns), "columns": []}),
            "clean_log": "[]",
            "table_version": 1,
        }
    )
    return table


def _call(name: str, **arguments) -> dict:
    return {"content": "", "tool_calls": [{"id": f"c_{name}", "name": name, "arguments": arguments}]}


def _final(text: str) -> dict:
    return {"content": text, "tool_calls": []}


def _model(*replies: dict):
    calls: list = []

    def fake(messages, tools_payload, model_id=None):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    fake.calls = calls
    return fake


def _insight_stub(*payloads: str):
    seen: list = []

    def fake(messages, model):
        seen.append(messages)
        return payloads[min(len(seen) - 1, len(payloads) - 1)]

    fake.seen = seen
    return fake


def _open_session(client, dataset_id: str = "d_test") -> str:
    return client.post("/sessions", json={"dataset_id": dataset_id}).json()["session"]["id"]


def test_synonym_question_reuses_sql_and_recomputes_insight(client, monkeypatch):
    """同义问法（精确键不中、向量命中）：复用 SQL、不跑工具，结论重算。"""
    _make_dataset()
    sid = _open_session(client)
    model = _model(_call("run_sql", sql=GOOD_SQL), _final("华北最高"))
    monkeypatch.setattr(llm, "chat_tools", model)
    stub = _insight_stub(INSIGHT, INSIGHT)
    monkeypatch.setattr(llm, "_complete", stub)
    first = client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"}).json()
    planned = len(model.calls)
    second = client.post(f"/sessions/{sid}/ask", json={"question": "各区域的销售总额是多少"}).json()
    assert first["cached"] is False
    assert second["cached"] is True and second["reused_sql"] is True
    assert second["steps"] == [] and second["sql"] == GOOD_SQL and second["row_count"] == 3
    assert len(model.calls) == planned  # 语义命中后不再进模型规划
    assert len(stub.seen) == 2  # 结论仍然重算
    assert client.get("/cache/stats").json()["semantic"]["entries"] >= 1


def test_unrelated_question_does_not_hit_cache(client, monkeypatch):
    """向量不相似就不命中：换个话题仍走模型。"""
    _make_dataset()
    sid = _open_session(client)
    model = _model(_call("run_sql", sql=GOOD_SQL), _final("华北最高"))
    monkeypatch.setattr(llm, "chat_tools", model)
    monkeypatch.setattr(llm, "_complete", _insight_stub(INSIGHT, INSIGHT))
    client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"})
    planned = len(model.calls)
    other = client.post(f"/sessions/{sid}/ask", json={"question": "有多少行"}).json()
    assert other["cached"] is False and other["reused_sql"] is False
    assert len(model.calls) > planned


def test_kb_semantic_recall_beats_fts(client, tmp_path: Path):
    """知识库语义召回：关键词完全对不上（FTS 命中 0 条）时向量仍能召回。"""
    _write_doc(tmp_path, "税.md", "增值税口径说明：含税与不含税两种算法，金额栏一律填含税值。")
    assert kb.import_path("kb")["imported"][0]["vectors"] == 1
    semantic = kb.search("税怎么算")
    assert [hit["source"] for hit in semantic["hits"]] == ["kb/税.md"]
    config.ENABLE_SEMANTIC_CACHE = False
    assert kb.search("税怎么算")["hits"] == []  # 关掉语义层就是 FTS5 的关键词口径


def test_semantic_off_falls_back_to_exact_key(client, monkeypatch):
    """ENABLE_SEMANTIC_CACHE=false：不调向量层，精确键照旧可用，统计里标明语义层关闭。"""
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_CACHE", False)
    _make_dataset()
    sid = _open_session(client)
    monkeypatch.setattr(llm, "chat_tools", _model(_call("run_sql", sql=GOOD_SQL), _final("华北最高")))
    monkeypatch.setattr(llm, "_complete", _insight_stub(INSIGHT, INSIGHT))
    client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"})
    again = client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"}).json()
    assert again["cached"] is True and cache.question_vector("各区域销售额") is None
    assert cache.lookup_semantic("d_test", 1, [1.0, 0.0, 0.0]) is None
    assert client.get("/cache/stats").json()["semantic"]["enabled"] is False


def test_missing_embedding_profile_degrades(client, monkeypatch):
    """没有 embedding 配置（llm.embed 抛 LLMUnavailable）：提问与知识库检索都退回关键词口径，不报错。"""
    _make_dataset()
    _write_doc(config.BASE_DIR / "tmp-unused-doc", "x.md", "占位") if False else None
    monkeypatch.setattr(llm, "embed", _raise_unavailable)
    sid = _open_session(client)
    monkeypatch.setattr(llm, "chat_tools", _model(_call("run_sql", sql=GOOD_SQL), _final("华北最高")))
    monkeypatch.setattr(llm, "_complete", _insight_stub(INSIGHT))
    body = client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"}).json()
    assert body["row_count"] == 3 and body["degraded"] == []
    assert cache.question_vector("各区域销售额") is None


def _raise_unavailable(texts: list[str]) -> list[list[float]]:
    raise llm.LLMUnavailable("没有可用的 embedding profile")


def _write_doc(root: Path, name: str, text: str) -> Path:
    """在临时知识库目录里写一份 md，返回路径。"""
    directory = root / "kb"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path
