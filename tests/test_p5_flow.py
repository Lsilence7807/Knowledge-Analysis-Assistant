# 文件：tests/test_p5_flow.py
# 作用：P5 前端闭环验收：首页与静态资源可取、上传→提问→结论全链路、/settings/models 不回传密钥且保存即生效
# 阶段：P5 前端闭环
# 依赖：json、pathlib、pytest、fastapi.testclient、app.config、app.llm、app.store
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.main import app
from app.services import llm, store

FIXTURE = Path(__file__).parent / "fixtures" / "dirty.csv"


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


def _upload(client) -> dict:
    """走 HTTP 上传夹具 CSV，返回画像（含 dataset_id）。"""
    response = client.post("/datasets", files={"file": ("dirty.csv", FIXTURE.read_bytes(), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()


def _count_sql() -> str:
    """用行数当结果表：不依赖清洗后的列名，结论里的数字能直接从表里回溯。"""
    return f"SELECT count(*) AS n FROM {store.list_datasets()[0]['table_name']}"


def _call(name: str, **arguments) -> dict:
    """agent 替身的一步：要求调用某个工具。"""
    return {"content": "", "tool_calls": [{"id": f"c_{name}", "name": name, "arguments": arguments}]}


def _final(text: str) -> dict:
    """agent 替身的收尾：不再调工具，直接给话。"""
    return {"content": text, "tool_calls": []}


def _chat_tools(*replies: dict):
    """chat_tools 替身：按顺序吐回复，用完重复最后一条。"""
    calls: list = []

    def fake(messages, tools_payload, model_id=None):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    fake.calls = calls
    return fake


def _insight_json(rows: int) -> str:
    """结论里的数字取自结果表，免得数字回溯把它写进 caveats。"""
    return json.dumps(
        {
            "summary": f"共 {rows} 行数据",
            "findings": [{"title": "行数", "detail": str(rows), "metric": "n", "direction": "flat"}],
            "anomalies": [],
            "suggestions": [{"action": "检查清洗日志", "rationale": "行数受清洗影响"}],
            "confidence": "medium",
            "caveats": ["示例结论，不是真实模型输出"],
        },
        ensure_ascii=False,
    )


def _reply(text: str, seen: list):
    """_complete 替身：固定吐一段文本，记录用过的 profile（验证换厂商是否生效）。"""

    def fake(messages, model):
        seen.append(model)
        return text

    return fake


def test_home_and_static_assets(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "echarts.min.js" in home.text and "提问" in home.text
    vendor = client.get("/vendor/echarts.min.js")
    assert vendor.status_code == 200 and len(vendor.content) > 100_000
    assert client.get("/health").json()["status"] == "ok"


def test_home_has_delete_dataset_button(client):
    """页面能删数据集（A 类补丁）：按钮与它调用的 DELETE 方法都在，改前端时别把它删掉。"""
    home = client.get("/")
    assert 'id="delete-dataset"' in home.text
    assert '"DELETE"' in home.text
    assert '"delete-dataset"' in home.text


def test_full_chain_upload_ask_insight(client, monkeypatch):
    dataset_id = _upload(client)["dataset_id"]
    sql = _count_sql()
    rows = client.post("/query", json={"sql": sql}).json()["rows"][0][0]
    monkeypatch.setattr(llm, "chat_tools", _chat_tools(_call("run_sql", sql=sql), _final("行数如上")))
    monkeypatch.setattr(llm, "_complete", _reply(_insight_json(rows), []))
    body = client.post("/ask", json={"dataset_id": dataset_id, "question": "一共几行"}).json()
    assert body["rows"] == [[rows]] and body["sql"] == sql
    assert len(body["steps"]) == 1 and body["steps"][0]["tool"] == "run_sql"
    assert body["insight"]["summary"] == f"共 {rows} 行数据"
    assert body["degraded"] == []
    for key in ("columns", "rows", "row_count", "truncated", "steps", "insight", "degraded", "message"):
        assert key in body


def test_settings_hides_key_and_takes_effect(client, monkeypatch):
    before = client.get("/settings/models")
    assert before.status_code == 200 and "test-key" not in before.text
    assert {m["id"]: m for m in before.json()["models"]}["local"]["has_key"] is True

    saved = client.put(
        "/settings/models",
        json={
            "id": "custom",
            "base_url": "https://example.invalid/v1",
            "model": "my-model",
            "api_key": "sk-page-key",
        },
    )
    assert saved.status_code == 200, saved.text
    assert "sk-page-key" not in saved.text
    profile = {m["id"]: m for m in saved.json()["models"]}["custom"]
    assert profile["model"] == "my-model" and profile["has_key"] is True
    assert config.local_settings()["api_keys"]["custom"] == "sk-page-key"
    assert config.local_settings()["default"] == "custom"
    assert config.api_key({"id": "custom"}) == "sk-page-key"  # 页面写的密钥优先于环境变量

    dataset_id = _upload(client)["dataset_id"]
    sql = _count_sql()
    rows = client.post("/query", json={"sql": sql}).json()["rows"][0][0]
    seen: list = []
    monkeypatch.setattr(llm, "chat_tools", _chat_tools(_call("run_sql", sql=sql), _final("行数如上")))
    monkeypatch.setattr(llm, "_complete", _reply(_insight_json(rows), seen))
    body = client.post("/ask", json={"dataset_id": dataset_id, "question": "一共几行"}).json()
    assert body["insight"] and body["degraded"] == []
    assert [model["id"] for model in seen] == ["custom"]  # 保存即生效：之后的模型调用都走新 profile


def test_settings_needs_id_base_url_and_model(client):
    assert client.put("/settings/models", json={"id": "x"}).status_code == 400


def test_key_can_be_cleared_from_page(client, monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    common = {"id": "custom", "base_url": "https://example.invalid/v1", "model": "my-model"}
    client.put("/settings/models", json={**common, "api_key": "sk-tmp"})
    cleared = client.put("/settings/models", json={**common, "api_key": ""}).json()
    assert {m["id"]: m for m in cleared["models"]}["custom"]["has_key"] is False
