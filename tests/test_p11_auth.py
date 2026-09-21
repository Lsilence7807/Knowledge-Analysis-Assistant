# 文件：tests/test_p11_auth.py
# 作用：F7（兼 P11）验收：登录/登出、401/413/429、cookie 篡改与过期、数据集归属校验、限流只在公开端生效、
#       以及「开了认证却没设口令就拒绝启动」的自检
# 阶段：F7 守卫与认证（兼 P11）
# 依赖：pytest、pandas、fastapi.testclient、backend/app/core/{config,security}.py、backend/app/main.py
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core import config, db, security
from app.main import create_app
from app.services import store

PASSWORD = "本地口令-123"
FRAME = pd.DataFrame({"region": ["华东", "华南"], "amount": [10, 11]})
CSV = "region,amount\n华东,10\n华南,11\n".encode()  # bytes 字面量只能放 ASCII，中文得编码进来


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离数据目录与本地密钥，开认证并设好口令，按公开端的方式重建应用（装上限流中间件）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    # 页面写过 config/local.json 的机器上，不隔离这个路径就会读进真实密钥
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "ENABLE_AUTH", True)
    monkeypatch.setattr(config, "AUTH_DISABLED", False)
    monkeypatch.setattr(config, "APP_PASSWORD_HASH", security.hash_password(PASSWORD))
    # 限流计数在进程里累计：每个用例从零开始，免得用例之间互相把额度吃掉
    security.limiter.reset()
    with TestClient(create_app()) as test_client:
        yield test_client


def _register_dataset(dataset_id: str = "d_test", owner: str | None = None) -> str:
    """登记一个数据集（DuckDB 表 + 元数据）；owner 传 None 就是改造前的老数据。"""
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
            "owner": owner,
        }
    )
    return table


def _login(client: TestClient, password: str = PASSWORD):
    """按口令登录，返回响应对象（cookie 落在 client 的 cookie jar 里）。"""
    return client.post("/login", json={"password": password})


def _cookie_names(client: TestClient) -> list:
    """客户端 cookie jar 里当前存着的 cookie 名（断言「没签发」用）。"""
    return [cookie.name for cookie in client.cookies.jar]


def test_health_and_capabilities_need_no_cookie(client):
    """§4.3：/health 与 /capabilities 认证列「否」，没登录也要能用（存活检查与降级判断靠它们）。"""
    assert client.get("/health").status_code == 200
    assert client.get("/capabilities").status_code == 200


@pytest.mark.parametrize("path", ["/datasets", "/tools", "/cache/stats"])
def test_protected_reads_without_cookie_are_401(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert "登录" in response.json()["detail"]


def test_writes_without_cookie_are_401(client):
    assert client.post("/query", json={"sql": "SELECT 1"}).status_code == 401
    assert client.post("/ask", json={"dataset_id": "d_test", "question": "多少"}).status_code == 401


def test_wrong_password_is_401_and_sets_no_cookie(client):
    response = _login(client, "错的")
    assert response.status_code == 401
    assert _cookie_names(client) == []  # 失败不签发任何 cookie


def test_login_then_logout_opens_and_closes_the_api(client):
    _register_dataset()
    login = _login(client)
    assert login.status_code == 200 and login.json()["user"] == security.USER_ID
    raw_cookie = login.headers["set-cookie"]
    assert "HttpOnly" in raw_cookie and "SameSite=lax" in raw_cookie.replace("samesite", "SameSite")
    # 本地（回环）不强制 Secure，否则 http 调试时浏览器根本不回传
    assert "Secure" not in raw_cookie
    assert client.get("/datasets").status_code == 200
    assert client.post("/logout").status_code == 200
    assert client.get("/datasets").status_code == 401


def test_tampered_cookie_is_401(client):
    """签名校验：改一个字符就该被 itsdangerous 判死，不能像改造前那样自己拼 HMAC。"""
    _login(client)
    token = client.cookies.get(security.COOKIE_NAME)
    assert token
    # 篡改第一位而不是最后一位：url-safe base64 的末位带填充位，翻末位有时会解出同一串字节（偶发假绿）
    client.cookies.set(security.COOKIE_NAME, ("a" if token[0] != "a" else "b") + token[1:])
    assert client.get("/datasets").status_code == 401


def test_expired_cookie_is_401(client, monkeypatch):
    _login(client)
    assert client.get("/datasets").status_code == 200
    monkeypatch.setattr(security, "SESSION_MAX_AGE_S", -1)  # 已经在世上待了 0 秒，负数即过期
    assert client.get("/datasets").status_code == 401


def test_oversize_upload_is_413(client, monkeypatch):
    """P11 自动项里的 413：开认证后上传上限照旧生效，且不再被 401 提前挡掉。"""
    _login(client)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)
    files = {"file": ("big.csv", CSV)}
    assert client.post("/datasets", files=files).status_code == 413


def test_login_attempts_are_rate_limited(client):
    """§7 登录失败限流：同一 IP 每分钟只放 5 次，第 6 次 429。"""
    codes = [_login(client, "错的口令").status_code for _ in range(6)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_global_limit_follows_config_and_only_when_public(client, monkeypatch):
    """RATE_LIMIT_PER_MIN 现在真的生效（K-010），且只算公开端：本地自用不该被自己的限流挡住。"""
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 2)
    codes = [client.get("/health").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    # 关掉认证（本地自用）后同一个应用里不再限流
    monkeypatch.setattr(config, "AUTH_DISABLED", True)
    assert [client.get("/health").status_code for _ in range(5)] == [200] * 5


def test_upload_records_owner_and_listing_keeps_it(client):
    _login(client)
    files = {"file": ("t.csv", CSV)}
    dataset_id = client.post("/datasets", files=files).json()["dataset_id"]
    assert store.get_dataset(dataset_id)["owner"] == security.USER_ID


def test_other_owners_dataset_is_404_but_legacy_rows_stay_readable(client):
    """§7 数据归属：别人的数据集按 404（不泄露存在性）；没记 owner 的老数据不能锁在门外。"""
    _register_dataset("d_mine", owner=security.USER_ID)
    _register_dataset("d_other", owner="someone_else")
    _register_dataset("d_legacy")
    _login(client)
    assert client.get("/datasets/d_mine/profile").status_code == 200
    assert client.get("/datasets/d_legacy/profile").status_code == 200
    assert client.get("/datasets/d_other/profile").status_code == 404
    assert client.post("/stats", json={"dataset_id": "d_other"}).status_code == 404


def test_auth_disabled_skips_login_entirely(client, monkeypatch):
    """F7 退化口径：AUTH_DISABLED=true（本地开发）时所有端点照旧免登录。"""
    monkeypatch.setattr(config, "AUTH_DISABLED", True)
    _register_dataset()
    assert client.get("/datasets").status_code == 200


def test_startup_refuses_when_password_is_missing(monkeypatch, tmp_path):
    """开了认证却没设口令：拒绝启动，也别放行成「看起来要登录」的样子。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.setattr(config, "APP_PASSWORD_HASH", "")
    monkeypatch.setattr(config, "ENABLE_AUTH", True)
    monkeypatch.setattr(config, "AUTH_DISABLED", False)
    with pytest.raises(RuntimeError, match="APP_PASSWORD_HASH"):
        with TestClient(create_app()):
            pass


def test_login_without_configured_password_is_503(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD_HASH", "")
    assert _login(client).status_code == 503


def test_framework_schema_routes_are_closed_when_auth_is_on(client):
    """开认证时 /openapi.json、/docs、/redoc 不再往外发 schema（它们直挂 app，绕开 api_router 的认证依赖）。

    §4.3 只放行 /health、/capabilities、/login、/logout：这三条被 SPA 兜底接住（HTML）或 503 都算关掉，
    只要不是「没有 cookie 也能拿到一坨 JSON schema」。
    """
    for path in ("/openapi.json", "/docs", "/redoc"):
        response = client.get(path)
        assert not response.headers.get("content-type", "").startswith("application/json"), path
