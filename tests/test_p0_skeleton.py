# 文件：tests/test_p0_skeleton.py
# 作用：P0 验收测试：健康检查、能力开关全 false、建表幂等、配置默认值、未知路径 404
# 阶段：P0 骨架与契约冻结
# 依赖：pytest、fastapi.testclient（依赖 httpx）
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, store
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把数据目录指向临时目录，避免污染项目 data/。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    with TestClient(app) as test_client:
        yield test_client


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_capabilities_all_false_at_p0(client):
    response = client.get("/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert len(body["capabilities"]) == 5
    assert set(body["capabilities"].values()) == {False}
    assert set(body["descriptions"]) == set(body["capabilities"])


def test_ensure_tables_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    store.ensure_tables()
    first = store.table_names()
    store.ensure_tables()
    second = store.table_names()
    assert first == second
    assert {"datasets", "tasks", "agent_steps", "trace_spans", "capability_log"} <= set(first)


def test_config_defaults():
    assert config.MAX_UPLOAD_MB == 50
    assert config.AGENT_MAX_STEPS == 6
    assert isinstance(config.ENABLE_AGENT, bool)
    assert config.SQLITE_PATH.name == "meta.sqlite"


def test_unknown_path_returns_404(client):
    assert client.get("/does-not-exist").status_code == 404
