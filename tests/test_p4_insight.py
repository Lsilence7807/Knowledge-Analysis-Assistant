# 文件：tests/test_p4_insight.py
# 作用：P4 洞察生成验收测试：字段齐全、数字回溯、缺字段与模型不可用的降级、/ask 两条路径都带结论
# 阶段：P4 洞察生成
# 依赖：json、pytest、pandas、fastapi.testclient、app.config、app.llm、app.store
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config, db, llm, store
from app.main import app

SAMPLE = pd.DataFrame({"region": ["华东", "华南", "华北"] * 4, "amount": [10, 11, 12] * 4})
SQL = "SELECT region, sum(amount) AS total FROM ds_d_test GROUP BY 1 ORDER BY 2 DESC"
TABLE = [["华北", 48], ["华南", 44], ["华东", 40]]
QUESTION = {"dataset_id": "d_test", "question": "各区销售额", "sql": SQL}
GOOD_INSIGHT = {
    "summary": "华北 48 最高，华东 40 最低，三区差距不大",
    "findings": [{"title": "华北最高", "detail": "48", "metric": "total", "direction": "up"}],
    "anomalies": [],
    "suggestions": [{"action": "核查华东的量价结构", "rationale": "40 最低"}],
    "confidence": "medium",
    "caveats": ["未含退货数据"],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "ENABLE_AGENT", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
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


def _reply(*payloads: str):
    """模型替身：按顺序吐给定文本，重复用最后一条；calls 记录调用次数。"""
    fake_calls: list = []

    def fake(messages, model):
        fake_calls.append(messages)
        return payloads[min(len(fake_calls) - 1, len(payloads) - 1)]

    fake.calls = fake_calls
    return fake


def _call(name: str, **arguments) -> dict:
    """agent 模型替身的一步：要求调用某个工具。"""
    return {"content": "", "tool_calls": [{"id": f"c_{name}", "name": name, "arguments": arguments}]}


def _final(text: str) -> dict:
    """agent 模型替身的收尾：不再调工具，直接给结论。"""
    return {"content": text, "tool_calls": []}


def _chat_tools(*replies: dict):
    """chat_tools 替身：按顺序吐回复，用完后重复最后一条。"""
    calls: list = []

    def fake(messages, tools_payload, model_id=None):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    fake.calls = calls
    return fake


def _good() -> str:
    return json.dumps(GOOD_INSIGHT, ensure_ascii=False)


def test_insight_route_returns_complete_fields(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(llm, "_complete", _reply(_good()))
    body = client.post("/insight", json=QUESTION).json()
    assert body["rows"] == TABLE and body["sql"] == SQL
    assert body["degraded"] == [] and body["message"] == ""
    result = body["insight"]
    assert set(result) == {"summary", "findings", "anomalies", "suggestions", "confidence", "caveats"}
    assert result["summary"] == GOOD_INSIGHT["summary"]
    assert result["findings"][0]["title"] == "华北最高"
    assert result["anomalies"] == [] and result["confidence"] == "medium"
    assert result["caveats"] == ["未含退货数据"]
    assert client.get("/capabilities").json()["capabilities"]["insight"] is True


def test_insight_missing_sql_is_422(client):
    """K-013：缺 sql 是 422（schema 层拦下），不再是 400 的手工判断。"""
    assert client.post("/insight", json={"dataset_id": "d_test", "question": "各区销售额"}).status_code == 422


def test_invented_number_lands_in_caveats(client, monkeypatch):
    _make_dataset(SAMPLE)
    reply = {**GOOD_INSIGHT, "summary": "华东 40 最低，另有 999999 的异常"}
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps(reply, ensure_ascii=False)))
    body = client.post("/insight", json=QUESTION).json()
    caveats = "；".join(body["insight"]["caveats"])
    assert "999999" in caveats and "40" not in caveats


def test_cited_findings_are_traced_without_literal_match(client, monkeypatch):
    """K-009：findings 都给了行号时，合计/环比这类表里没有原文的算式结果不再进 caveats。"""
    _make_dataset(SAMPLE)
    reply = {
        **GOOD_INSIGHT,
        "summary": "华北 48 最高，环比 -20%，三区合计 132",
        "findings": [
            {
                "title": "华北最高",
                "detail": "48，环比 -20%，三区合计 132",
                "metric": "total",
                "direction": "up",
                "row": 0,
                "column": "total",
            }
        ],
    }
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps(reply, ensure_ascii=False)))
    body = client.post("/insight", json=QUESTION).json()
    assert body["insight"]["caveats"] == ["未含退货数据"]


def test_cited_row_out_of_range_falls_back_to_literal_check(client, monkeypatch):
    """引用行号越界等于没引用：回到字面回溯，编造的数字仍进 caveats。"""
    _make_dataset(SAMPLE)
    reply = {
        **GOOD_INSIGHT,
        "summary": "另有 999999 的异常",
        "findings": [{**GOOD_INSIGHT["findings"][0], "row": 99}],
    }
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps(reply, ensure_ascii=False)))
    body = client.post("/insight", json=QUESTION).json()
    assert "999999" in "；".join(body["insight"]["caveats"])


def test_table_name_digits_are_not_suspicious(client, monkeypatch):
    """K-018：表名 `ds_<id>` 里的数字不是结论里的数字。"""
    _make_dataset(SAMPLE)
    reply = {
        **GOOD_INSIGHT,
        "summary": "华北 48 最高（取自表 ds_d_fc466e97）",
        "findings": [{"title": "华北最高", "detail": "48", "metric": "total", "direction": "up"}],
    }
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps(reply, ensure_ascii=False)))
    body = client.post("/insight", json=QUESTION).json()
    caveats = "；".join(body["insight"]["caveats"])
    assert caveats == "未含退货数据"


def test_datetime_column_does_not_break_insight(client, monkeypatch):
    """手工复验发现的 500：结果表里有 datetime 列时 json.dumps 抛 TypeError（K-021）。"""
    frame = pd.DataFrame({"name": ["a", "b"], "ts": pd.to_datetime(["2026-01-01", "2026-01-02"])})
    _make_dataset(frame)
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps(GOOD_INSIGHT, ensure_ascii=False)))
    response = client.post(
        "/insight",
        json={"dataset_id": "d_test", "question": "看看这表", "sql": "SELECT * FROM ds_d_test"},
    )
    assert response.status_code == 200
    assert response.json()["insight"]["summary"].startswith("华北")


def test_missing_field_degrades_to_contract(client, monkeypatch):
    _make_dataset(SAMPLE)
    broken = {key: value for key, value in GOOD_INSIGHT.items() if key != "confidence"}
    fake = _reply(json.dumps(broken, ensure_ascii=False))
    monkeypatch.setattr(llm, "_complete", fake)
    body = client.post("/insight", json=QUESTION).json()
    assert body["insight"] is None
    assert body["degraded"] == ["insight:contract"] and body["message"]
    assert body["rows"] == TABLE and body["sql"] == SQL
    assert len(fake.calls) == 2  # 契约不合只重试 1 次


def test_model_unavailable_keeps_table(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    body = client.post("/insight", json=QUESTION).json()
    assert body["insight"] is None and body["degraded"] == ["insight:llm"]
    assert body["rows"] == TABLE and "密钥" in body["message"]


def test_empty_result_skips_insight(client, monkeypatch):
    _make_dataset(SAMPLE)
    fake = _reply(_good())
    monkeypatch.setattr(llm, "_complete", fake)
    body = client.post(
        "/insight",
        json={"dataset_id": "d_test", "question": "空表怎么看", "sql": "SELECT region FROM ds_d_test WHERE amount < 0"},
    ).json()
    assert body["rows"] == [] and body["insight"] is None and body["degraded"] == []
    assert fake.calls == []  # 没有行就不浪费一次模型调用


def test_ask_carries_insight_on_agent_path(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(llm, "chat_tools", _chat_tools(_call("run_sql", sql=SQL), _final("各地区销售额如上")))
    monkeypatch.setattr(llm, "_complete", _reply(_good()))
    body = client.post("/ask", json={"dataset_id": "d_test", "question": "各区销售额"}).json()
    assert body["rows"] == TABLE and len(body["steps"]) == 1
    assert body["message"] == "各地区销售额如上"
    assert body["insight"]["summary"] == GOOD_INSIGHT["summary"]
    assert body["degraded"] == []


def test_ask_carries_insight_on_p3_path(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(config, "ENABLE_AGENT", False)
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps({"sql": SQL}), _good()))
    body = client.post("/ask", json={"dataset_id": "d_test", "question": "各区销售额"}).json()
    assert body["steps"] == [] and body["sql"] == SQL
    assert body["insight"]["confidence"] == "medium" and body["degraded"] == []


def test_ask_keeps_table_when_insight_degrades(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(llm, "chat_tools", _chat_tools(_call("run_sql", sql=SQL), _final("如上")))
    monkeypatch.setattr(config, "LLM_API_KEY", "")  # agent 走替身，insight 这一跳拿不到密钥
    body = client.post("/ask", json={"dataset_id": "d_test", "question": "各区销售额"}).json()
    assert body["sql"] == SQL and body["rows"] == TABLE
    assert body["degraded"] == ["insight:llm"] and body["insight"] is None
