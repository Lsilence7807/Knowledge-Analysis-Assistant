# 文件：tests/test_p3_nlu.py
# 作用：P3 自然语言转 SQL 验收测试：成功路径、缺密钥、非法输出、危险 SQL 的降级
# 阶段：P3 自然语言转 SQL
# 依赖：pytest、pandas、fastapi.testclient、app.llm、app.store
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config, db, llm, models, store
from app.main import app

SAMPLE = pd.DataFrame({"region": ["华东", "华南", "华北"] * 7, "amount": [10, 11, 12] * 7})
GOOD_SQL = "SELECT region, sum(amount) AS total FROM ds_d_test GROUP BY 1 ORDER BY 2 DESC"
QUESTION = {"dataset_id": "d_test", "question": "各区域销售额"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def with_key(monkeypatch):
    """假装本机配了密钥；模型调用本身由 _reply 替身接管，不出网。"""
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")


def _make_dataset(frame: pd.DataFrame, dataset_id: str = "d_test") -> str:
    """登记一个数据集（DuckDB 表 + sqlite 元数据），返回表名。"""
    store.ensure_tables()
    table = db.register_table(dataset_id, frame)
    store.insert_dataset(
        {
            "id": dataset_id, "name": "t.csv", "table_name": table, "rows": len(frame),
            "cols": len(frame.columns),
            "profile_json": json.dumps({"rows": len(frame), "cols": len(frame.columns), "columns": [
                {"name": name, "dtype": str(dtype), "null_count": 0} for name, dtype in frame.dtypes.items()
            ]}),
            "clean_log": "[]", "table_version": 1,
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


def test_ask_returns_result_table(client, with_key, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps({"sql": GOOD_SQL, "reason": "按区域求和"})))
    response = client.post("/ask", json=QUESTION)
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] == []
    assert body["sql"] == GOOD_SQL
    assert body["columns"] == ["region", "total"]
    assert body["rows"] == [["华北", 84], ["华南", 77], ["华东", 70]]
    assert body["task_id"].startswith("t_") and body["dataset_id"] == "d_test"
    assert body["cached"] is False and body["reused_sql"] is False


def test_ask_without_key_degrades_and_service_stays_usable(client, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == ["query:llm"]
    assert body["sql"] == "" and body["rows"] == []
    assert "模型密钥" in body["message"] and "config/local.json" in body["message"]
    kept = client.post("/query", json={"sql": "SELECT count(*) AS n FROM ds_d_test"})
    assert kept.json()["rows"] == [[21]]


def test_dangerous_sql_from_model_is_blocked_and_not_executed(client, with_key, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps({"sql": "DROP TABLE ds_d_test"})))
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == ["query:llm"]
    assert body["sql"] == ""
    assert "未通过校验" in body["message"]
    kept = client.post("/query", json={"sql": "SELECT count(*) AS n FROM ds_d_test"})
    assert kept.json()["rows"] == [[21]]


def test_invalid_json_retries_once_then_degrades(client, with_key, monkeypatch):
    _make_dataset(SAMPLE)
    fake = _reply("这根本不是 JSON")
    monkeypatch.setattr(llm, "_complete", fake)
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == ["query:llm"]
    assert len(fake.calls) == 2


def test_overlong_model_text_is_truncated_in_message(client, with_key, monkeypatch):
    _make_dataset(SAMPLE)
    monkeypatch.setattr(llm, "_complete", _reply("x" * 5000))
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == ["query:llm"]
    assert len(body["message"]) < 1000
    assert "x" * 1000 not in body["message"]


def test_need_clarify_degrades_with_readable_message(client, with_key, monkeypatch):
    _make_dataset(SAMPLE)
    reply = json.dumps({"need_clarify": True, "clarify": "请说明口径：销售额还是订单数"})
    monkeypatch.setattr(llm, "_complete", _reply(reply))
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == ["query:llm"]
    assert "口径" in body["message"]
    assert body["sql"] == ""


def test_ask_missing_dataset_is_404(client, with_key, monkeypatch):
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps({"sql": GOOD_SQL})))
    assert client.post("/ask", json={"dataset_id": "d_nope", "question": "x"}).status_code == 404


def test_ask_empty_question_is_400(client):
    _make_dataset(SAMPLE)
    assert client.post("/ask", json={"dataset_id": "d_test", "question": "  "}).status_code == 400


def test_page_key_wins_over_env_and_takes_effect_immediately(client, monkeypatch, tmp_path):
    """页面写入的本地密钥优先于环境变量，且保存后立即生效（不用重启）。"""
    _make_dataset(SAMPLE)
    local = tmp_path / "local.json"
    local.write_text(json.dumps({"api_keys": {"local": "page-key"}}), encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", local)
    monkeypatch.setattr(config, "LLM_API_KEY", "env-key")
    assert config.api_key(models.resolve()) == "page-key"
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps({"sql": GOOD_SQL})))
    assert client.post("/ask", json=QUESTION).json()["degraded"] == []


def test_api_key_falls_back_to_profile_env_then_generic(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "missing.json")
    monkeypatch.setattr(config, "LLM_API_KEY", "generic-key")
    monkeypatch.setenv("SOME_PROVIDER_KEY", "provider-key")
    assert config.api_key({"id": "x", "api_key_env": "SOME_PROVIDER_KEY"}) == "provider-key"
    assert config.api_key({"id": "x", "api_key_env": ""}) == "generic-key"
    broken = tmp_path / "broken.json"
    broken.write_text("{半截", encoding="utf-8")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", broken)
    assert config.api_key({"id": "x", "api_key_env": "SOME_PROVIDER_KEY"}) == "provider-key"


def test_page_added_provider_profile_is_used_by_ask(client, monkeypatch, tmp_path):
    """页面配任意 OpenAI 兼容厂商（base_url + 模型名 + 密钥）就能直接提问，不用改代码。"""
    _make_dataset(SAMPLE)
    local = tmp_path / "local.json"
    local.write_text(
        json.dumps(
            {
                "default": "my-glm",
                "models": [
                    {
                        "id": "my-glm", "provider": "openai_compatible",
                        "base_url": "https://open.bigmodel.cn/api/paas/v4",
                        "model": "glm-4-flash", "api_key_env": "GLM_API_KEY",
                        "purpose": ["sql", "insight"],
                    }
                ],
                "api_keys": {"my-glm": "page-key"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "LOCAL_SETTINGS", local)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    seen: dict = {}

    def fake(messages, model):
        seen.update(model)
        return json.dumps({"sql": GOOD_SQL})

    monkeypatch.setattr(llm, "_complete", fake)
    body = client.post("/ask", json=QUESTION).json()
    assert body["degraded"] == []
    assert seen["model"] == "glm-4-flash"
    assert seen["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert "my-glm" in [item["id"] for item in models.list_models()]
