# 文件：tests/test_p0_skeleton.py
# 作用：P0 验收测试：健康检查、无密钥时的能力可用性、建表幂等、配置默认值、未知路径 404
# 阶段：P0 骨架与契约冻结
# 依赖：pytest、fastapi.testclient（依赖 httpx）
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.main import app
from app.providers import kb, skills
from app.services import store


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


def test_capabilities_without_key_only_stats_available(client, monkeypatch):
    # 本机可能已经配了密钥（页面写入或 setx），这里显式关掉：测的是「没有密钥时哪些能力可用」，不是「这台机器没密钥」
    # K-022 之前断言是「全 false」；P2 补注册 stats（不依赖模型）后，无密钥时它必须 true、其余必须 false。
    # P6/P7 又补了 skills/kb（同样不依赖模型，只受各自 ENABLE_* 控制）：这里显式关掉这两个开关，
    # 断言只钉「无密钥时只有 stats 可用」而不钉条数——新增能力不必再来改这条测试（K-019 同款环境隔离）
    monkeypatch.setattr(config, "api_key", lambda: "")
    monkeypatch.setattr(skills, "ENABLED", False)
    monkeypatch.setattr(kb, "ENABLED", False)
    response = client.get("/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["capabilities"]["stats"] is True
    assert all(v is False for name, v in body["capabilities"].items() if name != "stats")
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


def test_unknown_path_serves_frontend(client):
    """未匹配路径交给前端（SPA 回退，F2b 起）：有构建产物回 index.html，没构建给可读提示。"""
    resp = client.get("/does-not-exist")
    if (config.FRONTEND_DIST / "index.html").is_file():
        assert resp.status_code == 200 and 'id="root"' in resp.text
    else:
        assert resp.status_code == 503
    # 接口层 404 不变：不存在的数据集仍按 §4.3 回 404（SPA 回退不许吃掉接口的报错）
    assert client.get("/datasets/no-such-id/profile").status_code == 404
