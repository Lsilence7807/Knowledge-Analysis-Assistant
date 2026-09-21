# 文件：tests/test_p15_sandbox.py
# 作用：P15 验收测试：沙箱里跑通自定义计算（注入数据集只读副本）、输出截断、工作目录清理、指标别名解析与未定义标注；
#       反例：import os、写文件、联网、fork 子进程、双下划线逃逸、死循环超时、巨量输出、内存爆炸、语法错、
#       空代码、数据集不存在、开关关闭降级、白名单外直调（K-023 口径）、给模块挂属性（K-051 口径）
# 阶段：P15 沙箱代码执行与指标语义层
# 依赖：json、time、contextlib、pytest、pandas、fastapi.testclient、backend/app/core/config.py、
#       backend/app/core/db.py、backend/app/main.py、backend/app/services/metrics.py、
#       backend/app/services/sandbox.py、backend/app/services/store.py、backend/app/services/tools.py
from __future__ import annotations

import json
import time
from contextlib import closing

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core import config, db
from app.main import app
from app.services import metrics, sandbox, store, tools

SAMPLE = pd.DataFrame({"region": ["华东", "华南", "华北"] * 4, "amount": [10, 11, 12] * 4})
SUM_AMOUNT = 132  # (10+11+12)*4：断言沙箱算的就是这份数据，不是别的
ESCAPES = [
    "import os\nresult = os.getcwd()",
    "from subprocess import run",
    "import socket",
    "import shutil",
    "import pathlib\npathlib.Path('x.txt').write_text('hi')",
    "open('x.txt', 'w').write('hi')",
    "__import__('os').system('echo hi')",
    "().__class__.__bases__",
    "import importlib",
    "import ctypes",
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """数据目录指向临时目录、本地密钥隔离、沙箱显式打开（默认是关的）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    # 本机页面配过密钥时，不隔离这个路径就会拿真实密钥出网（同 K-019）
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(sandbox, "ENABLED", True)
    with TestClient(app) as test_client:
        yield test_client


def _make_dataset(frame: pd.DataFrame = SAMPLE, dataset_id: str = "d_test") -> str:
    """登记一个数据集（DuckDB 表 + sqlite 元数据），返回表名（同 test_p13 的读法）。"""
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


def _run(client, code: str, **extra) -> dict:
    """走 HTTP 跑一段代码，返回响应对象（断言状态码用）。"""
    return client.post("/sandbox/run", json={"code": code, **extra})


def _capability_events(capability: str) -> list[str]:
    """读能力流水，供越权留痕断言用（同 test_p13/p7 的读法）。"""
    with closing(store.connect()) as conn:
        rows = conn.execute("SELECT event FROM capability_log WHERE capability = ?", (capability,)).fetchall()
    return [row["event"] for row in rows]


def test_custom_metric_is_computed_on_a_dataset_copy(client):
    _make_dataset()
    body = _run(client, "result = df.amount.sum()", dataset_id="d_test").json()
    assert body["ok"] is True, body["error"]
    assert str(SUM_AMOUNT) in body["output"]
    assert body["truncated"] is False and body["error"] == ""


def test_result_variable_can_hold_a_frame(client):
    _make_dataset()
    body = _run(client, 'result = df.groupby("region").amount.sum()', dataset_id="d_test").json()
    assert body["ok"] is True
    assert "华东" in body["output"] and "40" in body["output"]  # 华东四轮各 10


def test_code_without_dataset_still_runs(client):
    body = _run(client, "result = sum(range(10))").json()
    assert body["ok"] is True and "45" in body["output"]


def test_workdir_is_cleaned_up_after_run(client, tmp_path):
    _make_dataset()
    _run(client, "result = df.shape", dataset_id="d_test")
    assert list((tmp_path / "sandbox").iterdir()) == []


def test_dataset_copy_is_a_read_only_snapshot(client):
    _make_dataset()
    body = _run(client, 'df.loc[0, "amount"] = 9999\nresult = df.amount.sum()', dataset_id="d_test").json()
    # 副本在沙箱里确实被改了（一行的 10 变成 9999），源表一动不动——这才是「只读副本」的意思
    assert body["ok"] is True, body.get("error") or body.get("stderr")
    assert str(SUM_AMOUNT - 10 + 9999) in body["output"]
    assert db.exec_sql("SELECT sum(amount) AS total FROM ds_d_test")["rows"][0][0] == SUM_AMOUNT


def test_dataframe_item_assignment_is_allowed(client):
    """df["新列"] = ... 是数据计算里最常用的写法：F7 换 RestrictedPython 守卫时被误拦成

    `TypeError: object does not support item or slice assignment`（K-051），这里钉住它必须能跑。
    """
    _make_dataset()
    body = _run(client, 'df["毛利"] = df.amount * 0.3\nresult = int((df["毛利"]).sum())', dataset_id="d_test").json()
    assert body["ok"] is True, body.get("error") or body.get("stderr")
    assert str(int(SUM_AMOUNT * 0.3)) in body["output"]


def test_module_assignment_is_still_rejected(client):
    """放行内存写不等于放开一切：给模块（pd）挂属性仍要被拒，且给中文原因。"""
    body = _run(client, "pd.foo = 1\nresult = 1").json()
    assert body["ok"] is False
    assert "不允许给函数、方法、类或模块赋值" in body["stderr"]


def test_syntax_error_and_empty_code_are_rejected(client):
    assert _run(client, "def (").status_code == 400
    assert _run(client, "   ").status_code == 400


def test_missing_dataset_is_a_readable_rejection(client):
    response = _run(client, "result = 1", dataset_id="d_nope")
    assert response.status_code == 400 and "数据集不存在" in response.json()["detail"]


@pytest.mark.parametrize("code", ESCAPES)
def test_escapes_are_rejected_before_spawning(client, code):
    """清单 §3.3 沙箱逃逸里静态可判的那些：进子进程之前就得拒掉。"""
    with pytest.raises(sandbox.SandboxError):
        sandbox.run_code(code)
    assert _run(client, code).status_code == 400


def test_dead_loop_is_killed_by_timeout(client):
    started = time.monotonic()
    body = sandbox.run_code("while True:\n    pass", timeout_s=2)
    assert body["ok"] is False and "超时" in body["error"]
    assert time.monotonic() - started < 15


def test_huge_output_is_truncated(client):
    body = sandbox.run_code("for _ in range(40000):\n    print('x' * 50)", max_output=1000)
    assert body["truncated"] is True and len(body["output"]) == 1000


def test_memory_blowup_is_contained(client):
    body = sandbox.run_code("result = len(bytearray(10 ** 12))")
    assert body["ok"] is False and "MemoryError" in body["error"]
    # 内存炸掉的是子进程，服务本身还活着：紧接着再跑一段正常代码照样出结果
    assert sandbox.run_code("result = 1 + 1")["ok"] is True


def test_capability_reports_available_when_enabled(client):
    assert client.get("/capabilities").json()["capabilities"]["sandbox"] is True


def test_disabled_switch_degrades_without_error(client, monkeypatch):
    monkeypatch.setattr(sandbox, "ENABLED", False)
    assert client.get("/capabilities").json()["capabilities"]["sandbox"] is False
    assert _run(client, "result = 1").status_code == 503
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["run_code"]})
    with pytest.raises(tools.ToolError) as excinfo:
        tools.call("run_code", {"code": "result = 1"})
    assert "沙箱未启用" in str(excinfo.value)


def test_whitelist_gate_on_direct_tool_call(client, monkeypatch):
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["run_sql"]})
    with pytest.raises(tools.ToolError):
        tools.call("run_code", {"code": "result = 1"})
    assert "error" in _capability_events("tools")


def test_run_code_tool_computes_on_dataset_copy(client, monkeypatch):
    _make_dataset()
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["run_code"]})
    body = tools.call("run_code", {"code": "result = df.amount.mean()", "dataset_id": "d_test"})
    assert body["ok"] is True and "11" in body["output"]


def test_definitions_match_the_config_file(client):
    body = client.get("/metrics/definitions").json()
    on_disk = json.loads((config.BASE_DIR / "config" / "metrics.json").read_text(encoding="utf-8"))
    assert on_disk["definitions"] == body["definitions"]
    assert {item["id"] for item in body["definitions"]} == {"sales_amount", "order_count", "avg_order_value"}


def test_aliases_resolve_to_metric_ids():
    hits = metrics.resolve_metrics("华东区 GMV 环比降了两成，客单价也下来了")
    assert [hit["id"] for hit in hits] == ["sales_amount", "avg_order_value"]
    assert hits[0]["matched"] == "GMV"
    assert hits[0]["formula_sql"] == "sum(amount)"
    assert metrics.resolve_metrics("今天天气不错") == []


def test_undefined_metric_is_flagged_instead_of_invented():
    assert metrics.undefined_terms("退货率上升，毛利率继续下滑") == ["退货率", "毛利率"]
    # 定义里有的写法不算未定义（含从长词里切出来的子串）
    assert metrics.undefined_terms("销售额是多少") == []


def test_list_metrics_tool_returns_the_same_definitions(client, monkeypatch):
    monkeypatch.setattr(tools, "settings", lambda: {**tools.DEFAULT_SETTINGS, "allow": ["list_metrics"]})
    ids = [item["id"] for item in tools.call("list_metrics", {"dataset_id": "d_test"})["definitions"]]
    assert ids == ["sales_amount", "order_count", "avg_order_value"]


def test_tools_endpoint_lists_the_two_new_tools(client):
    body = client.get("/tools").json()
    assert {"list_metrics", "run_code"} <= set(body["allow"])
    assert body["kinds"]["run_code"] == "read"
