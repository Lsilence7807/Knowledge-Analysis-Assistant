# 文件：tests/test_p13_agent.py
# 作用：P13 Agent 循环验收测试：多跳/单跳、步骤落库、白名单与异常、单步截断、回落 P3、/tools；
#       A 类补丁加 tasks 落库（K-005）；B 类补丁 K-006 加每步超时一条、K-023 加越权直接调 call() 一条
# 阶段：P13 Agent 循环与工具调用
# 依赖：json、contextlib、pytest、pandas、fastapi.testclient、app.agent、app.llm、app.store、app.tools
from __future__ import annotations

import json
from contextlib import closing

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config, db, llm, store, tools
from app.main import app

SAMPLE = pd.DataFrame({"region": ["华东", "华南", "华北"] * 4, "amount": [10, 11, 12] * 4})
BIG = pd.DataFrame({"region": ["华东", "华南"] * 250, "amount": list(range(500))})
QUESTION = {"dataset_id": "d_test", "question": "先找下降最多的产品，再按渠道拆开"}
GOOD_SQL = "SELECT region, sum(amount) AS total FROM ds_d_test GROUP BY 1 ORDER BY 2 DESC"
SPLIT_SQL = "SELECT region, count(*) AS n FROM ds_d_test GROUP BY 1"
INSIGHT = json.dumps(
    {
        "summary": "华东区降幅最大，集中在直营",
        "findings": [{"title": "华东下降", "detail": "40", "metric": "total", "direction": "down"}],
        "anomalies": [],
        "suggestions": [{"action": "核查渠道库存", "rationale": "降幅集中"}],
        "confidence": "medium",
        "caveats": [],
    },
    ensure_ascii=False,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    # 本机页面配过密钥时，不隔离这个路径就会拿真实密钥出网（同 K-019）
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "ENABLE_AGENT", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    # P4 起 /ask 还会走一次 insight 的 chat_json（这里给合规替身，免得测试出网）
    monkeypatch.setattr(llm, "_complete", lambda messages, model: INSIGHT)
    with TestClient(app) as test_client:
        yield test_client


def _make_dataset(frame: pd.DataFrame, dataset_id: str = "d_test") -> str:
    """登记一个数据集（DuckDB 表 + sqlite 元数据），返回表名。"""
    store.ensure_tables()
    table = db.register_table(dataset_id, frame)
    store.insert_dataset(
        {
            "id": dataset_id,
            "name": "t.csv",
            "table_name": table,
            "rows": len(frame),
            "cols": len(frame.columns),
            "profile_json": json.dumps(
                {
                    "rows": len(frame),
                    "cols": len(frame.columns),
                    "columns": [
                        {"name": name, "dtype": str(dtype), "null_count": 0} for name, dtype in frame.dtypes.items()
                    ],
                }
            ),
            "clean_log": "[]",
            "table_version": 1,
        }
    )
    return table


def _call(name: str, **arguments) -> dict:
    """模型替身的一步：要求调用某个工具。"""
    return {"content": "", "tool_calls": [{"id": f"c_{name}", "name": name, "arguments": arguments}]}


def _final(text: str) -> dict:
    """模型替身的收尾：不再调工具，直接给结论。"""
    return {"content": text, "tool_calls": []}


def _model(*replies: dict):
    """chat_tools 替身：按顺序吐回复，用完后重复最后一条；seen 记下每轮收到的 messages。"""
    seen: list = []

    def fake(messages, tools_payload, model_id=None):
        seen.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return replies[min(len(seen) - 1, len(replies) - 1)]

    fake.seen = seen
    return fake


def _reply(*payloads: str):
    """模型替身：按顺序吐给定文本，重复用最后一条（给 insight 那一跳用）。"""
    calls: list = []

    def fake(messages, model):
        calls.append(messages)
        return payloads[min(len(calls) - 1, len(payloads) - 1)]

    fake.calls = calls
    return fake


def test_multi_hop_question_takes_two_steps(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(
        llm,
        "chat_tools",
        _model(
            _call("run_sql", sql=GOOD_SQL),
            _call("run_sql", sql=SPLIT_SQL),
            _final("华东区降幅最大，渠道上集中在直营"),
        ),
    )
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == []
    assert [step["tool"] for step in body["steps"]] == ["run_sql", "run_sql"]
    assert body["sql"] == SPLIT_SQL and body["columns"] == ["region", "n"]
    assert body["row_count"] == 3
    assert body["message"].startswith("华东区降幅最大")
    for step in body["steps"]:
        assert step["ok"] is True and isinstance(step["ms"], int) and len(step["args_digest"]) == 4


def test_single_hop_question_takes_one_step(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(llm, "chat_tools", _model(_call("run_sql", sql=GOOD_SQL), _final("按区域求和")))
    body = client.post("/ask", json=QUESTION).json()
    assert len(body["steps"]) == 1
    assert body["rows"] == [["华北", 48], ["华南", 44], ["华东", 40]]
    assert body["truncated"] is False


def test_steps_are_persisted_matching_response(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(
        llm,
        "chat_tools",
        _model(_call("run_sql", sql=GOOD_SQL), _call("run_sql", sql=SPLIT_SQL), _final("两步拿到结论")),
    )
    body = client.post("/ask", json=QUESTION).json()
    with closing(store.connect()) as conn:
        rows = conn.execute(
            "SELECT n, tool, ok, ms, rows FROM agent_steps WHERE task_id = ? ORDER BY n",
            (body["task_id"],),
        ).fetchall()
    assert len(rows) == len(body["steps"]) == 2
    assert [row["n"] for row in rows] == [1, 2]
    assert all(row["ok"] == 1 and row["tool"] == "run_sql" for row in rows)


def test_ask_writes_task_row(client, monkeypatch):
    """K-005：agent 路径也要落 tasks，带 session_id、问题原文与 degraded 快照。"""
    _make_dataset(SAMPLE)
    monkeypatch.setattr(llm, "chat_tools", _model(_call("run_sql", sql=GOOD_SQL), _final("一步拿到结论")))
    body = client.post("/ask", json={**QUESTION, "session_id": "s_1"}).json()
    with closing(store.connect()) as conn:
        rows = conn.execute(
            "SELECT id, dataset_id, session_id, kind, question, sql, status, degraded_json, error "
            "FROM tasks WHERE id = ?",
            (body["task_id"],),
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["id"], row["dataset_id"], row["session_id"], row["kind"]) == (body["task_id"], "d_test", "s_1", "ask")
    assert row["question"] == QUESTION["question"] and row["sql"] == GOOD_SQL
    assert row["status"] == "ok" and json.loads(row["degraded_json"]) == [] and row["error"] == ""


def test_tool_outside_whitelist_is_refused_and_loop_continues(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["run_sql"]})
    monkeypatch.setattr(
        llm,
        "chat_tools",
        _model(_call("describe_stats"), _call("run_sql", sql=GOOD_SQL), _final("换白名单内的工具拿到了结果")),
    )
    body = client.post("/ask", json=QUESTION).json()
    assert body["steps"][0]["tool"] == "describe_stats" and body["steps"][0]["ok"] is False
    assert "白名单" in body["steps"][0]["error"]
    assert len(body["steps"]) == 2 and body["sql"] == GOOD_SQL
    with closing(store.connect()) as conn:
        logged = [dict(row) for row in conn.execute("SELECT capability, event FROM capability_log")]
    assert {"capability": "agent", "event": "error"} in logged


def test_tools_call_refuses_outside_whitelist(client, monkeypatch):
    """白名单外的工具直接调 tools.call() 也要被挡住并留痕（P6/P8 起会有新的直接调用入口）。"""
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["run_sql"]})
    with pytest.raises(tools.ToolError) as excinfo:
        tools.call("describe_stats", {})
    assert "白名单" in str(excinfo.value)
    with closing(store.connect()) as conn:
        logged = [dict(row) for row in conn.execute("SELECT capability, event FROM capability_log")]
    assert {"capability": "tools", "event": "error"} in logged


def test_unknown_tool_name_does_not_crash(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(
        llm,
        "chat_tools",
        _model(_call("drop_everything"), _call("run_sql", sql=GOOD_SQL), _final("换用 run_sql 重试后拿到了结果")),
    )
    response = client.post("/ask", json=QUESTION)
    body = response.json()
    assert response.status_code == 200
    assert body["steps"][0]["ok"] is False and "没有这个工具" in body["steps"][0]["error"]
    assert body["row_count"] == 3


def test_budget_exhausted_returns_partial_steps(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(config, "AGENT_MAX_STEPS", 1)
    monkeypatch.setattr(
        llm,
        "chat_tools",
        _model(_call("run_sql", sql=GOOD_SQL), _call("run_sql", sql=SPLIT_SQL), _final("来不及说完")),
    )
    response = client.post("/ask", json=QUESTION)
    body = response.json()
    assert response.status_code == 200
    assert len(body["steps"]) == 1
    assert "步数" in body["message"]
    assert body["sql"] == GOOD_SQL and body["row_count"] == 3


def test_tool_error_marks_step_failed_and_keeps_200(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(
        llm,
        "chat_tools",
        _model(_call("run_sql", sql="DROP TABLE ds_d_test"), _final("这个查询被守卫拒了")),
    )
    response = client.post("/ask", json=QUESTION)
    body = response.json()
    assert response.status_code == 200
    assert body["steps"][0]["ok"] is False
    assert "只允许 SELECT" in body["steps"][0]["error"]
    assert body["rows"] == [] and body["sql"] == ""
    kept = client.post("/query", json={"sql": "SELECT count(*) AS n FROM ds_d_test"}).json()
    assert kept["rows"] == [[12]]


def test_large_step_result_is_capped_in_context_not_in_response(client, monkeypatch):
    """K-008：响应结果表按 /query 口径给足行数，回给模型的那份仍截到 200 行。"""
    _make_dataset(BIG, "d_big")
    fake = _model(_call("run_sql", sql="SELECT * FROM ds_d_big"), _final("明细 500 行都拿到了"))
    monkeypatch.setattr(llm, "chat_tools", fake)
    body = client.post("/ask", json={"dataset_id": "d_big", "question": "看明细"}).json()
    assert body["truncated"] is False and len(body["rows"]) == 500 and body["row_count"] == 500
    assert body["steps"][0]["rows"] == 500
    handed_back = json.dumps(fake.seen[1], ensure_ascii=False)
    assert '"rows"' not in handed_back and "只回列名与行数" in handed_back


def test_model_unavailable_degrades_without_crashing(client, monkeypatch):
    _make_dataset(SAMPLE)

    def boom(messages, tools_payload, model_id=None):
        raise llm.LLMUnavailable("未配置模型密钥（profile local）：在页面配一次")

    monkeypatch.setattr(llm, "chat_tools", boom)
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == ["query:llm"] and body["rows"] == [] and body["steps"] == []
    kept = client.post("/query", json={"sql": "SELECT count(*) AS n FROM ds_d_test"}).json()
    assert kept["rows"] == [[12]]


def test_agent_disabled_falls_back_to_p3_single_hop(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(config, "ENABLE_AGENT", False)
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps({"sql": GOOD_SQL}), INSIGHT))
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == [] and body["steps"] == []
    assert body["sql"] == GOOD_SQL and body["row_count"] == 3


def test_tools_route_lists_registry_and_whitelist(client):
    body = client.get("/tools").json()
    assert [item["function"]["name"] for item in body["tools"]] == ["describe_stats", "detect_anomalies", "run_sql"]
    assert body["allow"] == ["run_sql", "describe_stats", "detect_anomalies"]
    assert body["max_steps"] == 6 and body["max_rows_per_step"] == 200
    assert body["kinds"]["run_sql"] == "read"


def test_step_timeouts_come_from_tools_json(monkeypatch):
    """K-006：三个工具都按 tools.json 的 timeouts 走，describe 不再吃 exec_sql 的默认 10s。"""
    seen: list[int] = []
    monkeypatch.setattr(
        tools,
        "settings",
        lambda: {**tools.DEFAULT_SETTINGS, "timeouts": {"run_sql": 3, "describe_stats": 7, "detect_anomalies": 9}},
    )
    monkeypatch.setattr(
        db,
        "exec_sql",
        lambda sql, **kwargs: seen.append(kwargs["timeout_s"]) or {"rows": [], "columns": [], "truncated": False},
    )
    tools.call("run_sql", {"sql": "SELECT 1"})
    tools.call("describe_stats", {})
    tools.call("detect_anomalies", {})
    assert seen == [3, 7, 9]
