# 文件：tests/test_p9_models.py
# 作用：F3（P9）模型层验收：models.json → LiteLLM Router 的模型组与回退链、结构化输出、模型全挂降级、direct 回落
# 阶段：F3 模型层换 LiteLLM
# 依赖：pytest、respx、httpx、pandas、fastapi.testclient、app.services.llm、app.services.llm_models
from __future__ import annotations

import json

import httpx
import pandas as pd
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core import config, db
from app.main import app
from app.services import llm, llm_models, store

CHAT = r"https://api.example.com/v1/chat/completions"
FRAME = pd.DataFrame({"region": ["华东", "华南"] * 3, "amount": [1, 2] * 3})


class Verdict(BaseModel):
    """结构化输出的目标契约（P3/P4 用的都是这一套 pydantic 模型）。"""

    sql: str
    reason: str


def _profile(pid: str, purpose: list[str], **extra) -> dict:
    profile = {
        "id": pid,
        "provider": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "model": f"{pid}-model",
        "api_key_env": "LLM_API_KEY",
        "purpose": purpose,
    }
    return {**profile, **extra}


def _write_models(profiles: list[dict], **extra) -> None:
    config.MODELS_CONFIG.write_text(
        json.dumps({"default": profiles[0]["id"], "models": profiles, **extra}, ensure_ascii=False), encoding="utf-8"
    )


def _write_keys(keys: dict) -> None:
    config.LOCAL_SETTINGS.write_text(json.dumps({"api_keys": keys}), encoding="utf-8")


def _completion(content: str) -> dict:
    """OpenAI 兼容的补全响应体（respx 桩用）。"""
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """配置全指向 tmp：models.json 由用例自己写，密钥走 local.json；Router 缓存每次清空。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "MODELS_CONFIG", tmp_path / "models.json")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_BACKEND", "litellm")
    monkeypatch.setattr(config, "ENABLE_AGENT", False)
    monkeypatch.setattr(llm, "_router", None)
    monkeypatch.setattr(llm, "_router_key", "")


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


def test_purpose_becomes_model_group():
    """purpose 声明的组里有多个 profile 时用组名（组内挑 + 回退），只有一个时直接用 profile id。"""
    _write_models([_profile("first", ["sql"]), _profile("second", ["sql"])])
    _write_keys({"first": "sk-1", "second": "sk-2"})

    names = [item["model_name"] for item in llm_models.deployments()]
    assert names == ["first", "sql", "second", "sql"]
    assert llm_models.purpose_groups() == {"sql": ["first", "second"]}
    assert llm._group_name({"id": "first", "purpose": ["sql"]}) == "sql"

    _write_models([_profile("solo", ["sql", "insight"])])
    _write_keys({"solo": "sk-1"})
    assert llm._group_name({"id": "solo", "purpose": ["sql"]}) == "solo"


def test_missing_key_is_marked_unavailable_and_kept_out_of_router():
    """没配密钥的 profile 标 unavailable，且不进 Router（免得每次调用白等超时）。"""
    _write_models([_profile("withkey", ["sql"]), _profile("nokey", ["sql"])])
    _write_keys({"withkey": "sk-1"})

    status = {item["id"]: item["status"] for item in llm_models.availability()}
    assert status == {"withkey": "available", "nokey": "unavailable"}
    assert [item["model_name"] for item in llm_models.deployments()] == ["withkey", "sql"]


def test_fallback_chain_from_top_level_and_profile():
    """顶层 fallback 兜所有 profile；profile 自己的 fallback 接在前面。"""
    _write_models(
        [_profile("first", ["sql"], fallback=["second"]), _profile("second", ["sql"]), _profile("third", ["sql"])],
        fallback=["third"],
    )
    chains = {next(iter(item)): list(item.values())[0] for item in llm_models.fallbacks()}
    assert chains["first"] == ["second", "third"]
    assert chains["second"] == ["third"]
    assert llm_models.fallback_names({"id": "second"}) == ["third"]
    assert llm_models.fallback_names({"id": "third", "fallback": ["first"]}) == ["first"]


def test_chat_json_goes_through_litellm_router():
    """chat_json 走 LiteLLM Router：拿到 JSON 文本后按 pydantic 模型校验。"""
    _write_models([_profile("first", ["sql"])])
    _write_keys({"first": "sk-1"})
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as mock:
        route = mock.route(method="POST", url__regex=r".*/chat/completions").mock(
            return_value=httpx.Response(200, json=_completion('{"sql": "SELECT 1", "reason": "一步"}'))
        )
        verdict = llm.chat_json("你是 SQL 助手", "随便问", Verdict)
    assert verdict.sql == "SELECT 1"
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["model"] == "first-model"


def test_ask_degrades_when_all_models_down(client):
    """模型全挂仍是 HTTP 200 + degraded，表格路径不受影响（前端靠这个降级）。"""
    _write_models([_profile("first", ["sql"]), _profile("second", ["sql"])], fallback=["second"])
    _write_keys({"first": "sk-1", "second": "sk-2"})
    _make_dataset()
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as mock:
        mock.route(method="POST", url__regex=r".*/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": {"message": "boom", "type": "server_error"}})
        )
        response = client.post("/ask", json={"dataset_id": "d_test", "question": "各区域销售额"})
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] == ["query:llm"]
    assert body["rows"] == []
    assert "boom" in body["message"] or "模型" in body["message"]


def test_direct_backend_replaces_router(client, monkeypatch):
    """LLM_BACKEND=direct：原 OpenAI 单厂商客户端仍可用（LiteLLM 出问题时的退路）。"""
    monkeypatch.setattr(config, "LLM_BACKEND", "direct")
    _write_models([_profile("first", ["sql"])])
    _write_keys({"first": "sk-1"})
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as mock:
        mock.route(method="POST", url__regex=r".*/chat/completions").mock(
            return_value=httpx.Response(200, json=_completion('{"sql": "SELECT 2", "reason": "direct"}'))
        )
        verdict = llm.chat_json("你是 SQL 助手", "随便问", Verdict)
    assert verdict.sql == "SELECT 2"
    assert llm._router is None  # direct 路径不建 Router


def test_settings_models_marks_unavailable(client):
    """/settings/models 仍只回「密钥配没配」，前端据此标 unavailable。"""
    _write_models([_profile("first", ["sql"]), _profile("nokey", ["sql"])])
    _write_keys({"first": "sk-1"})
    body = client.get("/settings/models").json()
    assert {item["id"]: item["has_key"] for item in body["models"]} == {"first": True, "nokey": False}
