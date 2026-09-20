# 文件：tests/test_p17_stream.py
# 作用：P17 流式验收——帧序与 done 唯一、sql 早于 token、降级仍 200、断连取消、慢客户端不拖垮其他请求
# 阶段：P17 流式输出
# 依赖：pytest、asyncio、httpx、pandas、fastapi.testclient、app/{config,db,llm,main,store,stream}
from __future__ import annotations

import asyncio
import json
import time
from contextlib import closing

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.core import config, db
from app.main import app
from app.services import llm, store, stream

FRAME = pd.DataFrame({"region": ["华东", "华南", "华北"] * 4, "amount": [10, 11, 12] * 4})
SQL = "SELECT region, sum(amount) AS total FROM ds_d_test GROUP BY 1 ORDER BY 2 DESC"
QUESTION = "各区域销售额"
# 与 test_p3 同款：/ask 链路里 insight 还要走一次 chat_json，替身得答这一跳
INSIGHT = json.dumps(
    {
        "summary": "华南最高",
        "findings": [{"title": "华南最高", "detail": "44", "metric": "total", "direction": "up", "row": 1}],
        "anomalies": [],
        "suggestions": [{"action": "核查华东", "rationale": "最低"}],
        "confidence": "medium",
        "caveats": [],
    },
    ensure_ascii=False,
)


@pytest.fixture()
def dataset(tmp_path, monkeypatch):
    """隔离数据目录、开流式、钉住单跳通道，并登记一个数据集；返回数据集 id。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "meta.sqlite")
    monkeypatch.setattr(config, "DUCKDB_PATH", tmp_path / "analytics.duckdb")
    # 页面写过 config/local.json 的机器上，不隔离这个路径就会拿真实密钥出网
    monkeypatch.setattr(config, "LOCAL_SETTINGS", tmp_path / "local.json")
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "ENABLE_AGENT", False)
    monkeypatch.setattr(stream, "ENABLED", True)
    store.ensure_tables()
    table = db.register_table("d_test", FRAME)
    store.insert_dataset(
        {
            "id": "d_test",
            "name": "t.csv",
            "table_name": table,
            "rows": len(FRAME),
            "cols": len(FRAME.columns),
            "profile_json": json.dumps(
                {
                    "rows": len(FRAME),
                    "cols": len(FRAME.columns),
                    "columns": [
                        {"name": name, "dtype": str(dtype), "null_count": 0} for name, dtype in FRAME.dtypes.items()
                    ],
                }
            ),
            "clean_log": "[]",
            "table_version": 1,
        }
    )
    return "d_test"


def _url() -> str:
    return f"/ask/stream?dataset_id=d_test&question={QUESTION}"


def _reply(*payloads: str):
    """模型替身（_complete）：按顺序吐给定文本，用完重复最后一条；calls 记录调用次数。"""
    seen: list = []

    def fake(messages, model):
        seen.append(messages)
        return payloads[min(len(seen) - 1, len(payloads) - 1)]

    fake.calls = seen
    return fake


def _stub_stream(*pieces: str):
    """llm.stream_text 替身：逐段吐回给定片段；calls 记录被调用了几次。"""
    seen: list = []

    async def fake(system, user, model_id=None):
        seen.append(user)
        for piece in pieces:
            yield piece

    fake.calls = seen
    return fake


def _chat_tools(*payloads: dict):
    """chat_tools 替身：按顺序吐回复，用完重复最后一条。"""
    seen: list = []

    def fake(messages, tools, model_id=None):
        seen.append(messages)
        return payloads[min(len(seen) - 1, len(payloads) - 1)]

    fake.calls = seen
    return fake


def _call(name: str, **arguments) -> dict:
    """模型替身的一步：要求调用某个工具（形状与 agent.chat_tools 的返回一致）。"""
    return {"content": "", "tool_calls": [{"id": f"c_{name}", "name": name, "arguments": arguments}]}


def _final(text: str) -> dict:
    """模型替身的收尾：不再调工具，直接给结论。"""
    return {"content": text, "tool_calls": []}


def _frames(text: str) -> list[tuple[str, dict]]:
    """把响应体拆成 (事件名, data) 列表。"""
    out: list[tuple[str, dict]] = []
    for raw in text.split("\n\n"):
        event, payload = "", None
        for line in raw.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        if event:
            out.append((event, payload or {}))
    return out


def _data(events: list[tuple[str, dict]], name: str, default=None):
    """取第一个同名帧的 data。"""
    for event, data in events:
        if event == name:
            return data
    return default


def _canceled_rows() -> list:
    """查 tasks 表里记了 canceled 的行：断连必须留痕，否则查不出模型调用停没停。"""
    with closing(store.connect()) as conn:
        return conn.execute("SELECT id, degraded_json FROM tasks WHERE degraded_json LIKE '%canceled%'").fetchall()


def test_stream_disabled_returns_503(dataset, monkeypatch):
    """§5 退化行为：ENABLE_STREAM=false 时前端回落整段，端点直接给可读的 503。"""
    monkeypatch.setattr(stream, "ENABLED", False)
    with TestClient(app) as client:
        response = client.get(_url())
    assert response.status_code == 503
    assert "ENABLE_STREAM" in response.json()["detail"]


def test_bad_request_stays_http_not_stream(dataset, monkeypatch):
    """数据集不存在、问题为空：开流之前就报错，不从流里猜（HTTP 语义不退化）。"""
    with TestClient(app) as client:
        missing = client.get("/ask/stream?dataset_id=d_none&question=x")
        empty = client.get("/ask/stream?dataset_id=d_test&question=+")
    assert missing.status_code == 404
    assert empty.status_code == 400


def test_frame_order_done_unique_and_sql_before_token(dataset, monkeypatch):
    """§4.11：plan → sql → row_count → token → insight → trace → done，sql 早于 token，done 最后且唯一。"""
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps({"sql": SQL}), INSIGHT))
    fake = _stub_stream("华南", "最高，", "华东最低")
    monkeypatch.setattr(llm, "stream_text", fake)
    with TestClient(app) as client:
        response = client.get(_url())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _frames(response.text)
    names = [name for name, _ in events]
    assert names[0] == "plan"
    assert names.count("done") == 1 and names[-1] == "done"
    assert names.index("sql") < names.index("row_count") < names.index("token")
    assert names.index("token") < names.index("insight") < names.index("trace") < names.index("done")
    assert [data["text"] for name, data in events if name == "token"] == ["华南", "最高，", "华东最低"]
    assert len(fake.calls) == 1
    # 行数帧要带整张结果表：前端只有这一条流，表格与图表都靠它
    table = _data(events, "row_count")
    assert table["columns"] == ["region", "total"] and len(table["rows"]) == 3 and table["row_count"] == 3
    assert _data(events, "insight")["insight"]["summary"] == "华南最高"


def test_agent_path_keeps_frame_order(dataset, monkeypatch):
    """P13 的 agent 路径也走同一条流：步骤进 trace 帧，帧序不变。"""
    monkeypatch.setattr(config, "ENABLE_AGENT", True)
    monkeypatch.setattr(
        llm,
        "chat_tools",
        _chat_tools(_call("run_sql", sql=SQL), _final("如上")),
    )
    monkeypatch.setattr(llm, "_complete", _reply(INSIGHT))
    monkeypatch.setattr(llm, "stream_text", _stub_stream("华南最高"))
    with TestClient(app) as client:
        response = client.get(_url())
    events = _frames(response.text)
    names = [name for name, _ in events]
    assert names[0] == "plan" and names[-1] == "done" and names.count("done") == 1
    assert _data(events, "plan")["agent"] is True
    assert _data(events, "sql")["sql"] == SQL
    assert [step["tool"] for step in _data(events, "trace")["steps"]] == ["run_sql"]


def test_prose_failure_degrades_but_keeps_insight(dataset, monkeypatch):
    """说话那一跳失败只插 degraded 帧：结果表与结构化结论照常给，HTTP 仍 200。"""
    monkeypatch.setattr(llm, "_complete", _reply(json.dumps({"sql": SQL}), INSIGHT))

    async def broken(system, user, model_id=None):
        raise llm.LLMUnavailable("未配置模型密钥")
        yield ""  # pragma: no cover

    monkeypatch.setattr(llm, "stream_text", broken)
    with TestClient(app) as client:
        response = client.get(_url())
    assert response.status_code == 200
    events = _frames(response.text)
    names = [name for name, _ in events]
    assert names.count("done") == 1 and names[-1] == "done"
    assert "stream:llm" in [data["reason"] for name, data in events if name == "degraded"]
    assert _data(events, "done")["degraded"] == ["stream:llm"]
    assert _data(events, "insight")["insight"]["summary"] == "华南最高"


class _Gone:
    """is_disconnected 替身：TestClient/ASGITransport 都是把响应收完才回 http.disconnect，
    真实 socket 断开在这层造不出来，所以断连这条直接驱动生成器验。after 表示第几次问才算断开。"""

    def __init__(self, after: int = 0):
        self.after = after
        self.calls = 0

    async def is_disconnected(self) -> bool:
        self.calls += 1
        self._is_disconnected = self.calls > self.after
        return self._is_disconnected


def test_disconnect_before_pipeline_records_canceled_and_skips_model(dataset, monkeypatch):
    """断连后不再起模型调用，并落一条 degraded=stream:canceled 的记录（§4.11）。"""
    fake = _stub_stream("不该出现")
    monkeypatch.setattr(llm, "stream_text", fake)

    async def scenario() -> str:
        gen = app_main._stream_frames(_Gone(), QUESTION, None, "d_test", store.get_dataset("d_test"))
        first = await anext(gen)
        with pytest.raises(StopAsyncIteration):
            await anext(gen)
        return first

    assert "event: plan" in asyncio.run(scenario())
    assert fake.calls == []
    assert _canceled_rows()


def test_stream_cancellation_records_canceled(dataset, monkeypatch):
    """Starlette 收到 http.disconnect 会取消生成器：取消时也要留 canceled 的记录。"""
    monkeypatch.setattr(llm, "stream_text", _stub_stream("片段"))

    async def scenario() -> None:
        gen = app_main._stream_frames(_Gone(after=99), QUESTION, None, "d_test", store.get_dataset("d_test"))
        await anext(gen)  # plan 帧
        with pytest.raises(asyncio.CancelledError):
            await gen.athrow(asyncio.CancelledError())

    asyncio.run(scenario())
    assert _canceled_rows()


def test_slow_client_does_not_block_other_requests(dataset, monkeypatch):
    """慢客户端占着一条没读完的流时，别的请求照常拿到 200（§2 P17 反例）。"""
    monkeypatch.setattr(llm, "stream_text", _stub_stream(*["片段"] * 20))

    async def scenario() -> tuple[int, float]:
        gen = app_main._stream_frames(_Gone(after=99), QUESTION, None, "d_test", store.get_dataset("d_test"))
        for _ in range(2):  # 慢客户端只取两帧就不再读了，流停在半路
            await anext(gen)
        began = time.perf_counter()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://p17") as other:
            response = await other.get("/health")
        elapsed = time.perf_counter() - began
        await gen.aclose()
        return response.status_code, elapsed

    status, elapsed = asyncio.run(scenario())
    assert status == 200
    assert elapsed < 0.5, f"慢客户端占着流时 /health 用了 {elapsed:.3f}s"
