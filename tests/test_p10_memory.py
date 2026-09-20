# 文件：tests/test_p10_memory.py
# 作用：F4（P10）验收：会话生命周期、轮次上下文（K-012）、知识库片段进结论（K-034）、
#       精确缓存复用与失效、步骤轨迹、缓存统计
# 阶段：F4 Agent 与记忆换 LangGraph
# 依赖：pytest、pandas、fastapi.testclient、app.services.{memory,cache,llm,store,registry}、app.main
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core import config, db
from app.main import app
from app.services import cache, llm, memory, registry, store

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


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """隔离数据目录、元数据库、模型配置与 checkpointer：本文件测的是会话与缓存本身。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "CHECKPOINT_PATH", tmp_path / "checkpoints.sqlite")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(memory, "_saver", None)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_dataset(dataset_id: str = "d_test") -> str:
    """登记一个数据集（DuckDB 表 + sqlite 元数据），返回表名。"""
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
    """chat_tools 替身：按顺序吐回复，用完后重复最后一条；calls 记调用次数。"""
    calls: list = []

    def fake(messages, tools_payload, model_id=None):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    fake.calls = calls
    return fake


def _insight_stub(*payloads: str):
    """chat_json 那一跳的替身（走 llm._complete）；seen 记下每轮收到的 messages。"""
    seen: list = []

    def fake(messages, model):
        seen.append(messages)
        return payloads[min(len(seen) - 1, len(payloads) - 1)]

    fake.seen = seen
    return fake


def _open_session(client, dataset_id: str = "d_test") -> str:
    response = client.post("/sessions", json={"dataset_id": dataset_id, "title": "看看销售"})
    assert response.status_code == 200
    return response.json()["session"]["id"]


def test_session_lifecycle(client):
    """开会话 → 列不了不存在的会话（404）→ 删掉并回删了多少轮次。"""
    _make_dataset()
    sid = _open_session(client)
    assert sid.startswith("s_")
    assert client.get(f"/sessions/{sid}/steps").json() == {"session_id": sid, "steps": []}
    assert client.post("/sessions", json={"dataset_id": "d_nope"}).status_code == 404
    assert client.post(f"/sessions/{sid}/ask", json={"question": "  "}).status_code == 400
    assert client.delete(f"/sessions/{sid}").json() == {"session_id": sid, "deleted_turns": 0}
    assert client.get(f"/sessions/{sid}/steps").status_code == 404


def test_turns_feed_next_round_context(client, monkeypatch):
    """K-012：第一轮的问与 SQL 进 turns，第二轮结论节点真的拿到它（prompt 里有「补充上下文」）。"""
    _make_dataset()
    sid = _open_session(client)
    monkeypatch.setattr(
        llm,
        "chat_tools",
        _model(
            _call("run_sql", sql=GOOD_SQL),
            _final("华北最高"),
            _call("run_sql", sql=GOOD_SQL),
            _final("华东也不高"),
        ),
    )
    stub = _insight_stub(INSIGHT, INSIGHT)
    monkeypatch.setattr(llm, "_complete", stub)
    assert client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"}).json()["row_count"] == 3
    assert memory.context(sid) == [f"问：各区域销售额｜SQL：{GOOD_SQL}"]
    client.post(f"/sessions/{sid}/ask", json={"question": "那华东呢"})
    second_prompt = json.dumps(stub.seen[1], ensure_ascii=False)
    assert "补充上下文" in second_prompt and "各区域销售额" in second_prompt and GOOD_SQL in second_prompt


def test_provider_context_reaches_insight(client, monkeypatch):
    """K-034：启用的知识库片段进结论节点，不再是「未检索到知识库口径说明」。"""

    class FakeKB:
        @staticmethod
        def search(query: str, limit: int = 3) -> dict:
            return {"hits": [{"content": "销售额按含税口径统计", "source": "经营口径.md"}]}

    real_get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: FakeKB if name == "kb" else real_get(name))
    _make_dataset()
    sid = _open_session(client)
    monkeypatch.setattr(llm, "chat_tools", _model(_call("run_sql", sql=GOOD_SQL), _final("华北最高")))
    stub = _insight_stub(INSIGHT)
    monkeypatch.setattr(llm, "_complete", stub)
    client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"})
    assert "含税口径" in json.dumps(stub.seen[0], ensure_ascii=False)


def test_exact_cache_reuses_sql_and_recomputes_insight(client, monkeypatch):
    """同会话问同一个问题：第二次复用 SQL、不跑工具，结论重算（§4.7）。"""
    _make_dataset()
    sid = _open_session(client)
    model = _model(_call("run_sql", sql=GOOD_SQL), _final("华北最高"))
    monkeypatch.setattr(llm, "chat_tools", model)
    stub = _insight_stub(INSIGHT, INSIGHT)
    monkeypatch.setattr(llm, "_complete", stub)
    first = client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"}).json()
    planned = len(model.calls)  # 第一轮：规划 → 工具 → 再规划收尾
    second = client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额！"}).json()
    assert first["cached"] is False and first["reused_sql"] is False
    assert second["cached"] is True and second["reused_sql"] is True
    assert second["steps"] == [] and second["sql"] == GOOD_SQL and second["row_count"] == 3
    assert len(model.calls) == planned  # 第二次没再进模型规划
    assert len(stub.seen) == 2  # 结论仍然重算
    assert client.get("/cache/stats").json()["exact"]["entries"] == 1
    assert client.get("/cache/stats").json()["exact"]["hits"] >= 1


def test_cache_invalidated_by_table_version(client, monkeypatch):
    """表版本变了就不命中（重导入后旧 SQL 不能复用）。"""
    _make_dataset()
    sid = _open_session(client)
    monkeypatch.setattr(llm, "chat_tools", _model(_call("run_sql", sql=GOOD_SQL), _final("华北最高")))
    monkeypatch.setattr(llm, "_complete", _insight_stub(INSIGHT))
    client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"})
    with closing_conn() as conn:
        conn.execute("UPDATE datasets SET table_version = 2 WHERE id = 'd_test'")
        conn.commit()
    again = client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"}).json()
    assert again["cached"] is False
    assert cache.invalidate("d_test") == 1


def closing_conn():
    """元数据库连接（用例里直接改表版本用）。"""
    from contextlib import closing

    return closing(store.connect())


def test_steps_track_stay_queryable_by_session(client, monkeypatch):
    """会话的步骤轨迹按 tasks.session_id 串起来（P13 落的表，P10 只是加一条读路由）。"""
    _make_dataset()
    sid = _open_session(client)
    monkeypatch.setattr(llm, "chat_tools", _model(_call("run_sql", sql=GOOD_SQL), _final("华北最高")))
    monkeypatch.setattr(llm, "_complete", _insight_stub(INSIGHT))
    body = client.post(f"/sessions/{sid}/ask", json={"question": "各区域销售额"}).json()
    steps = client.get(f"/sessions/{sid}/steps").json()["steps"]
    assert [step["n"] for step in steps] == [1]
    assert steps[0]["task_id"] == body["task_id"] and steps[0]["tool"] == "run_sql"


def test_memory_off_degrades_to_single_round(client, monkeypatch):
    """ENABLE_MEMORY=false：不再给上下文，单轮提问照常可用。"""
    monkeypatch.setattr(config, "ENABLE_MEMORY", False)
    _make_dataset()
    monkeypatch.setattr(llm, "chat_tools", _model(_final("没有工具也能答")))
    monkeypatch.setattr(llm, "_complete", _insight_stub(INSIGHT))
    assert memory.agent_context("各区域销售额", "s_whatever") == ""
    body = client.post("/ask", json={"dataset_id": "d_test", "question": "各区域销售额"}).json()
    assert body["message"] == "没有工具也能答"
    assert registry.get("memory") is None
