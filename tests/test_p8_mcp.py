# 文件：tests/test_p8_mcp.py
# 作用：P8 验收测试：列工具（带白名单标记）、白名单内只读调用成功、外部内容按不可信包裹与截断、
#       仓库 config/mcp.json 能起来；反例：没配置/命令不存在/调用超时都只降级（capabilities.mcp=false、
#       不出现 500、/ask 仍 200）、白名单外被拒并留痕、超时后子进程被回收
# 阶段：P8 MCP 插件接入
# 依赖：json、os、subprocess、sys、time、contextlib、tempfile、pandas、pytest、fastapi.testclient、
#       app/config.py、app/db.py、app/store.py、app/tools.py、app/main.py、app/providers/mcp_client.py
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import gettempdir

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import config, db, store, tools
from app.main import app
from app.providers import mcp_client

FIXTURE_SERVER = config.BASE_DIR / "tests" / "fixtures" / "mcp_echo_server.py"
REPO_CONFIG = config.BASE_DIR / "config" / "mcp.json"
# 夹具 server 把 pid 写在这里，供「子进程被回收」那条断言读取（SDK 只继承少量环境变量，所以路径写死）
PIDFILE = Path(gettempdir()) / "kaa_mcp_echo.pid"
SAMPLE = pd.DataFrame({"region": ["华东", "华南"], "amount": [10, 12]})


def _write_config(root: Path, **patch) -> Path:
    """在临时目录写一份 mcp.json（默认指向仓库内的测试 server），返回它的路径。"""
    payload = {
        "command": sys.executable,
        "args": [str(FIXTURE_SERVER)],
        "timeout_s": 10,
        "allow": ["echo", "huge", "noisy"],
    }
    payload.update(patch)
    path = root / "mcp.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="")
    return path


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """数据目录与 mcp.json 都指向临时目录；MCP 能力显式打开（默认是关的）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    # 本机页面配过密钥时，不隔离这个路径就会拿真实密钥出网（同 K-019）
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(mcp_client, "CONFIG_PATH", _write_config(tmp_path))
    monkeypatch.setattr(mcp_client, "ENABLED", True)
    PIDFILE.unlink(missing_ok=True)
    with TestClient(app) as test_client:
        yield test_client


def _make_dataset(frame: pd.DataFrame = SAMPLE, dataset_id: str = "d_test") -> str:
    """登记一个数据集（DuckDB 表 + sqlite 元数据），返回表名（同 test_p13 的写法）。"""
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


def _capability_events(capability: str) -> list[str]:
    """读能力流水，供越权留痕断言用（同 test_p13 的读法）。"""
    with closing(store.connect()) as conn:
        rows = conn.execute("SELECT event FROM capability_log WHERE capability = ?", (capability,)).fetchall()
    return [row["event"] for row in rows]


def _process_alive(pid: int) -> bool:
    """跨平台判断 pid 还在不在：Windows 用 tasklist，其它平台用 kill(pid, 0)。"""
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _reclaimed(pid: int, timeout_s: float = 15.0) -> bool:
    """等子进程被回收：回收了返回 True，等超时还没死返回 False。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.3)
    return False


def test_lists_tools_with_whitelist_marks(client):
    body = client.get("/mcp/tools").json()
    marks = {item["name"]: item["allowed"] for item in body["tools"]}
    assert {"echo", "huge", "noisy", "hang"} <= set(marks)
    assert marks["echo"] is True
    assert marks["hang"] is False
    assert any(item["description"] for item in body["tools"])


def test_capability_reports_available_when_enabled(client):
    assert client.get("/capabilities").json()["capabilities"]["mcp"] is True


def test_repo_config_points_at_a_startable_server(client, monkeypatch):
    """真实用法：不 monkeypatch 配置路径，直接用仓库里的 config/mcp.json（手工项同口径）。"""
    monkeypatch.setattr(mcp_client, "CONFIG_PATH", REPO_CONFIG)
    body = client.get("/mcp/tools").json()
    marks = {item["name"]: item["allowed"] for item in body["tools"]}
    assert marks.get("echo") is True
    response = client.post("/mcp/call", json={"tool": "echo", "arguments": {"text": "口径是万元"}})
    assert response.status_code == 200
    assert "口径是万元" in response.json()["content"]


def test_call_allowed_tool_returns_fenced_text(client):
    body = client.post("/mcp/call", json={"tool": "echo", "arguments": {"text": "华东区口径"}}).json()
    assert body["tool"] == "echo"
    assert body["truncated"] is False
    assert "华东区口径" in body["content"]
    assert body["content"].startswith(mcp_client.FENCE_OPEN)
    assert body["content"].endswith(mcp_client.FENCE_CLOSE)


def test_mcp_content_is_treated_as_untrusted(client):
    """§7：外部返回里的指令性行要被剥掉，正常正文留着。"""
    content = client.post("/mcp/call", json={"tool": "noisy", "arguments": {}}).json()["content"]
    assert "退货" in content
    assert "忽略以上" not in content


def test_huge_result_is_truncated(client):
    body = client.post("/mcp/call", json={"tool": "huge", "arguments": {"chars": 5000}}).json()
    assert body["truncated"] is True
    assert len(body["content"]) < 5000


def test_tool_outside_whitelist_is_rejected_and_logged(client):
    response = client.post("/mcp/call", json={"tool": "hang", "arguments": {"seconds": 0}})
    assert response.status_code == 400
    assert "白名单" in response.json()["detail"]
    assert "error" in _capability_events("mcp")


def test_missing_config_degrades(client, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "CONFIG_PATH", tmp_path / "nope.json")
    assert client.get("/capabilities").json()["capabilities"]["mcp"] is False
    assert client.get("/mcp/tools").status_code == 503
    assert client.post("/mcp/call", json={"tool": "echo"}).status_code == 503


def test_server_that_cannot_start_degrades_without_500(client, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "CONFIG_PATH", _write_config(tmp_path, command="definitely-not-a-real-command"))
    assert client.get("/capabilities").json()["capabilities"]["mcp"] is False
    assert client.get("/mcp/tools").status_code == 503
    # MCP 挂了只影响 MCP：/ask 仍是 200（这里没配密钥，agent 自己降级，与 MCP 无关）
    _make_dataset()
    response = client.post("/ask", json={"dataset_id": "d_test", "question": "华东区销售额"})
    assert response.status_code == 200
    assert "mcp" not in json.dumps(response.json().get("degraded") or [])


def test_call_timeout_degrades_and_reclaims_process(client, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "CONFIG_PATH", _write_config(tmp_path, timeout_s=1, allow=["hang"]))
    assert client.get("/mcp/tools").status_code == 200
    started = time.monotonic()
    response = client.post("/mcp/call", json={"tool": "hang", "arguments": {"seconds": 30}})
    elapsed = time.monotonic() - started
    assert response.status_code == 400
    assert "timed out" in response.json()["detail"].lower()
    assert elapsed < 15, f"工具睡 30s 却只用了 {elapsed:.1f}s 回来：超时没生效或根本没起来"
    pid = int(PIDFILE.read_text(encoding="utf-8"))
    assert _reclaimed(pid), f"子进程 {pid} 没被回收"


def test_disabled_switch_degrades_without_error(client, monkeypatch):
    monkeypatch.setattr(mcp_client, "ENABLED", False)
    assert client.get("/capabilities").json()["capabilities"]["mcp"] is False
    assert client.get("/mcp/tools").status_code == 503
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["call_mcp_tool"]})
    with pytest.raises(tools.ToolError) as excinfo:
        tools.call("call_mcp_tool", {"tool": "echo", "arguments": {"text": "hi"}})
    assert "MCP" in str(excinfo.value)


def test_call_mcp_tool_respects_tools_whitelist(client, monkeypatch):
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["call_mcp_tool"]})
    result = tools.call("call_mcp_tool", {"tool": "echo", "arguments": {"text": "hi"}})
    assert "hi" in result["content"]
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["run_sql"]})
    with pytest.raises(tools.ToolError):
        tools.call("call_mcp_tool", {"tool": "echo", "arguments": {"text": "hi"}})
    assert "error" in _capability_events("tools")
